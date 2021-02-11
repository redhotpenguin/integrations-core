import json
import os
import time
from concurrent.futures.thread import ThreadPoolExecutor

import psycopg2
from cachetools import TTLCache

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent

from datadog_checks.base import is_affirmative
from datadog_checks.base.log import get_check_logger
from datadog_checks.base.utils.db.sql import compute_exec_plan_signature, compute_sql_signature
from datadog_checks.base.utils.db.utils import ConstantRateLimiter, resolve_db_host
from datadog_checks.base.utils.db.statement_samples import statement_samples_client

VALID_EXPLAIN_STATEMENTS = frozenset({'select', 'table', 'delete', 'insert', 'replace', 'update'})

# columns from pg_stat_activity which correspond to attributes common to all databases and are therefore stored in
# under other standard keys
pg_stat_activity_sample_exclude_keys = {
    # we process & obfuscate this separately
    'query',
    # stored separately
    'application_name',
    'datname',
    'usename',
    'client_addr',
    'client_hostname',
    'client_port',
}


class PostgresStatementSamples(object):
    executor = ThreadPoolExecutor()

    """Collects telemetry for SQL statements"""

    def __init__(self, check, config):
        self._check = check
        self._config = config
        self._log = get_check_logger()
        self._activity_last_query_start = None
        self._last_check_run = 0
        self._collection_loop_future = None
        self._tags = None
        self._tags_str = None
        self._service = "postgres"
        self._db_hostname = resolve_db_host(self._config.host)
        self._enabled = is_affirmative(self._config.statement_samples_config.get('enabled', False))
        self._debug = is_affirmative(self._config.statement_samples_config.get('debug', False))
        self._run_sync = is_affirmative(self._config.statement_samples_config.get('run_sync', False))
        self._rate_limiter = ConstantRateLimiter(
            float(self._config.statement_samples_config.get('collections_per_second', 1)))
        self._explain_function = self._config.statement_samples_config.get('explain_function',
                                                                           'datadog.explain_statement')

        # explained_statements_cache: limit how often we try to re-explain the same query
        self._explained_statements_cache = TTLCache(
            maxsize=int(self._config.statement_samples_config.get('explained_statements_cache_maxsize', 5000)),
            ttl=60 * 60 / int(self._config.statement_samples_config.get('explained_statements_per_hour_per_query', 60))
        )

        # seen_samples_cache: limit the ingestion rate per (query_signature, plan_signature)
        self._seen_samples_cache = TTLCache(
            # assuming ~60 bytes per entry (query & plan signature, key hash, 4 pointers (ordered dict), expiry time)
            # total size: 10k * 60 = 0.6 Mb
            maxsize=int(self._config.statement_samples_config.get('seen_samples_cache_maxsize', 10000)),
            ttl=60 * 60 / int(self._config.statement_samples_config.get('samples_per_hour_per_query', 15))
        )

    def run_sampler(self, tags):
        """
        start the sampler thread if not already running
        :param tags:
        :return:
        """
        if not self._enabled:
            self._log.debug("skipping statement samples as it's not enabled")
            return
        self._tags = tags
        self._tags_str = ','.join(self._tags)
        for t in self._tags:
            if t.startswith('service:'):
                self._service = t[len('service:'):]
        # store the last check run time so we can detect when the check has stopped running
        self._last_check_run = time.time()
        if self._run_sync or is_affirmative(os.environ.get('DBM_STATEMENT_SAMPLER_RUN_SYNC', "false")):
            self._log.debug("running statement sampler synchronously")
            self._collect_statement_samples()
        elif self._collection_loop_future is None or not self._collection_loop_future.running():
            self._log.info("starting postgres statement sampler")
            self._collection_loop_future = PostgresStatementSamples.executor.submit(self._collection_loop)
        else:
            self._log.debug("postgres statement sampler already running")

    def _get_new_pg_stat_activity(self, db):
        start_time = time.time()
        query = """
        SELECT * FROM {pg_stat_activity_view}
        WHERE datname = %s
        AND coalesce(TRIM(query), '') != ''
        AND query_start is not Null
        """.format(
            pg_stat_activity_view=self._config.pg_stat_activity_view
        )
        db.rollback()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            if self._activity_last_query_start:
                cursor.execute(query + " AND query_start > %s", (self._config.dbname, self._activity_last_query_start,))
            else:
                cursor.execute(query, (self._config.dbname,))
            rows = cursor.fetchall()

        self._check.histogram("dd.postgres.get_new_pg_stat_activity.time", (time.time() - start_time) * 1000,
                              tags=self._tags)
        self._check.histogram("dd.postgres.get_new_pg_stat_activity.rows", len(rows), tags=self._tags)

        for r in rows:
            if r['query'] and r['datname']:
                if self._activity_last_query_start is None or r['query_start'] > self._activity_last_query_start:
                    self._activity_last_query_start = r['query_start']
                yield r

    def _collection_loop(self):
        try:
            while True:
                if time.time() - self._last_check_run > self._config.min_collection_interval * 2:
                    self._log.info("sampler collection_loop stopping due to check inactivity")
                    self._check.count("dd.postgres.statement_samples.collection_loop_inactive_stop", 1, tags=self._tags)
                    break
                self._collect_statement_samples()
        except Exception as e:
            self._log.exception("statement sample collection loop failure")
            self._check.count("dd.postgres.statement_samples.error", 1,
                              tags=self._tags + ["error:collection-loop-failure-{}".format(type(e))])

    def _collect_statement_samples(self):
        self._rate_limiter.sleep()

        start_time = time.time()

        samples = self._get_new_pg_stat_activity(self._check.db)
        events = self._explain_pg_stat_activity(self._check.db, samples)
        submitted_count = statement_samples_client.submit_events(events)

        elapsed_ms = (time.time() - start_time) * 1000
        self._check.histogram("dd.postgres.collect_statement_samples.time", elapsed_ms, tags=self._tags)
        self._check.count("dd.postgres.collect_statement_samples.events_submitted.count", submitted_count,
                          tags=self._tags)
        self._check.gauge("dd.postgres.collect_statement_samples.seen_samples_cache.len", len(self._seen_samples_cache),
                          tags=self._tags)
        self._check.gauge("dd.postgres.collect_statement_samples.explained_statements_cache.len",
                          len(self._explained_statements_cache), tags=self._tags)

    def _can_obfuscate_statement(self, statement):
        if statement == '<insufficient privilege>':
            self._log.warn("Insufficient privilege to collect statement.")
            self._check.count("dd.postgres.statement_samples.error", 1,
                              tags=self._tags + ["error:insufficient-privilege"])
            return False
        if statement.startswith('SELECT {}'.format(self._explain_function)):
            return False
        if statement.startswith('autovacuum:'):
            return False
        return True

    def _can_explain_statement(self, statement):
        # TODO: cleaner query cleaning to strip comments, etc.
        if not self._can_obfuscate_statement(statement):
            return False
        if statement.strip().split(' ', 1)[0].lower() not in VALID_EXPLAIN_STATEMENTS:
            return False
        return True

    def _run_explain(self, db, statement):
        # TODO: see if there is default schema issue with postgres as well like mysql?
        if not self._can_explain_statement(statement):
            return
        with db.cursor() as cursor:
            try:
                start_time = time.time()
                cursor.execute(
                    """SELECT {explain_function}($stmt${statement}$stmt$)""".format(
                        explain_function=self._explain_function, statement=statement
                    )
                )
                result = cursor.fetchone()
                self._check.histogram("dd.postgres.run_explain.time", (time.time() - start_time) * 1000,
                                      tags=self._tags)
            except psycopg2.errors.UndefinedFunction:
                self._log.warn(
                    "Failed to collect execution plan due to undefined explain_function: %s.",
                    self._explain_function,
                )
                self._check.count("dd.postgres.statement_samples.error", 1,
                                  tags=self._tags + ["error:explain-undefined-function"])
                return None
            except Exception as e:
                self._log.error("failed to collect execution plan for query='%s'. (%s): %s", statement, type(e), e)
                self._check.count("dd.postgres.statement_samples.error", 1,
                                  tags=self._tags + ["error:explain-{}".format(type(e))])
                return None
        if not result or len(result) < 1 or len(result[0]) < 1:
            return None
        return result[0][0]

    def _explain_pg_stat_activity(self, db, samples):
        for row in samples:
            original_statement = row['query']
            if not self._can_obfuscate_statement(original_statement):
                continue

            try:
                obfuscated_statement = datadog_agent.obfuscate_sql(original_statement)
            except Exception as e:
                self._log.debug("failed to obfuscate statement: %s", e)
                self._check.count("dd.postgres.statement_samples.error", 1, tags=self._tags + ["error:sql-obfuscate"])
                continue

            # limit the rate of explains done to the database
            query_signature = compute_sql_signature(obfuscated_statement)
            if query_signature in self._explained_statements_cache:
                continue
            self._explained_statements_cache[query_signature] = True

            # Plans have several important signatures to tag events with. Note that for postgres, the
            # query_signature and resource_hash will be the same value.
            # - `plan_signature` - hash computed from the normalized JSON plan to group identical plan trees
            # - `resource_hash` - hash computed off the raw sql text to match apm resources
            # - `query_signature` - hash computed from the raw sql text to match query metrics
            plan_dict = self._run_explain(db, original_statement)
            plan, normalized_plan, obfuscated_plan, plan_signature, plan_cost = None, None, None, None, None
            if plan_dict:
                plan = json.dumps(plan_dict)
                normalized_plan = datadog_agent.obfuscate_sql_exec_plan(plan, normalize=True)
                obfuscated_plan = datadog_agent.obfuscate_sql_exec_plan(plan)
                plan_signature = compute_exec_plan_signature(normalized_plan)
                plan_cost = (plan_dict.get('Plan', {}).get('Total Cost', 0.0) or 0.0)

            statement_plan_sig = (query_signature, plan_signature)
            if statement_plan_sig not in self._seen_samples_cache:
                self._seen_samples_cache[statement_plan_sig] = True
                event = {
                    # the timestamp for activity events is the time at which they were collected
                    "timestamp": time.time() * 1000,
                    "host": self._db_hostname,
                    "service": self._service,
                    "ddsource": "postgres",
                    "ddtags": self._tags_str,
                    # no duration with postgres because these are in-progress, not complete events
                    # "duration": ?,
                    "network": {
                        "client": {
                            "ip": row.get('client_addr', None),
                            "port": row.get('client_port', None),
                            "hostname": row.get('client_hostname', None)
                        }
                    },
                    "db": {
                        "instance": row.get('datname', None),
                        "plan": {
                            "definition": obfuscated_plan,
                            "cost": plan_cost,
                            "signature": plan_signature
                        },
                        "query_signature": query_signature,
                        "resource_hash": query_signature,
                        "application": row.get('application_name', None),
                        "user": row['usename'],
                        "statement": obfuscated_statement
                    },
                    'postgres': {k: v for k, v in row.items() if k not in pg_stat_activity_sample_exclude_keys},
                }
                if self._debug:
                    event['db']['debug'] = {
                        'original_plan': plan,
                        'normalized_plan': normalized_plan,
                        'original_statement': original_statement,
                    }
                yield event
