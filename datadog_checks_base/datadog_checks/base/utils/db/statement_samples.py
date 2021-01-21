import datetime
import decimal
import itertools
import json
import logging

import requests
from requests.adapters import HTTPAdapter, Retry

try:
    import datadog_agent

    using_stub_datadog_agent = False
except ImportError:
    from ....stubs import datadog_agent

    using_stub_datadog_agent = True

LOGGER = logging.getLogger(__file__)


class EventEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        return super(EventEncoder, self).default(o)


def chunks(items, n):
    it = iter(items)
    while True:
        chunk = tuple(itertools.islice(it, n))
        if not chunk:
            return
        yield chunk


def _new_logs_session(api_key):
    http = requests.Session()
    http.mount("https://",
               HTTPAdapter(max_retries=Retry(connect=2, read=2, redirect=2, status=2, method_whitelist=['POST'])))
    http.headers.update({'DD-API-KEY': api_key})
    return http


def _logs_input_url(host):
    if host.endswith("."):
        host = host[:-1]
    if not host.startswith("https://"):
        host = "https://" + host
    return host + "/v1/input"


default_logs_url = "http-intake.logs.datadoghq.com"
default_dbm_url = "dbquery-http-intake.logs.datadoghq.com"


def _load_event_endpoints_from_config(config_prefix, default_url):
    """
    Returns a list of requests sessions and their endpoint urls [(http, url), ...]
    Requests sessions are initialized the first time this is called and reused thereafter
    :return: list of (http, url)
    # TODO: support other logs endpoint config options use_http, use_compression, compression_level

    :param config_prefix:
    :param default_url:
    :return:
    """
    url = _logs_input_url(datadog_agent.get_config('{}.dd_url'.format(config_prefix)) or default_url)
    endpoints = [(_new_logs_session(datadog_agent.get_config('api_key')), url)]
    LOGGER.debug("initializing event endpoints from %s. url=%s", config_prefix, url)

    for additional_endpoint in datadog_agent.get_config('{}.additional_endpoints'.format(config_prefix)) or []:
        api_key, host = additional_endpoint.get('api_key'), additional_endpoint.get('host')
        missing_keys = [k for k, v in [('api_key', api_key), ('host', host)] if not v]
        if missing_keys:
            LOGGER.warning("invalid event endpoint found in %s.additional_endpoints. missing required keys %s",
                           config_prefix, ', '.join(missing_keys))
            continue
        url = _logs_input_url(host)
        endpoints.append((_new_logs_session(api_key), url))
        LOGGER.debug("initializing additional event endpoint from %s. url=%s", config_prefix, url)

    return endpoints


logs_common_keys = {
    'ddtags',
    'host',
    'service',
    'ddsource',
    'timestamp'
}


def _to_logs_event(e):
    """
    Converts a database query track event to a logs event
    """
    m = {k: v for k, v in e.items() if k in logs_common_keys}
    m['message'] = {k: v for k, v in e.items() if k not in logs_common_keys}
    m['hostname'] = m['host']
    del m['host']
    return m


class StatementSamplesClient:
    def __init__(self):
        endpoints = _load_event_endpoints_from_config("database_monitoring_config", default_dbm_url)
        if datadog_agent.get_config('database_monitoring_config.double_write_to_logs'):
            LOGGER.debug("DBM double writing to logs enabled")
            endpoints.extend(_load_event_endpoints_from_config("logs_config", default_logs_url))
        self._endpoints = endpoints

    def submit_events(self, events):
        """
        Submit the execution plan events to the event intake
        https://docs.datadoghq.com/api/v1/logs/#send-logs
        """

        for chunk in chunks(events, 100):
            for http, url in self._endpoints:
                is_dbquery = 'dbquery' in url
                try:
                    r = http.request('post', url,
                                     data=json.dumps([_to_logs_event(e) if not is_dbquery else e for e in chunk],
                                                     cls=EventEncoder),
                                     timeout=5,
                                     headers={'Content-Type': 'application/json'})
                    r.raise_for_status()
                    LOGGER.debug("submitted %s statement samples to %s", len(chunk), url)
                except requests.HTTPError as e:
                    LOGGER.warning("failed to submit statement samples to %s: %s", url, e)
                except Exception:
                    LOGGER.exception("failed to submit statement samples to %s", url)


class StubStatementSamplesClient:
    def __init__(self):
        self._events = []

    def submit_events(self, events):
        self._events.extend(events)


statement_samples_client = StubStatementSamplesClient() if using_stub_datadog_agent else StatementSamplesClient()
