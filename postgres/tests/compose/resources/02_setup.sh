#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" datadog_test <<-EOSQL
    CREATE EXTENSION pg_stat_statements SCHEMA public;
    GRANT SELECT ON pg_stat_statements TO datadog;

    CREATE OR REPLACE FUNCTION public.explain_statement (
       l_query text,
       out explain JSON
    )
    RETURNS SETOF JSON AS
    \$body\$
    BEGIN
        RETURN QUERY EXECUTE 'EXPLAIN (FORMAT JSON) ' || l_query;
    END;
    \$body\$
    LANGUAGE 'plpgsql'
    VOLATILE
    RETURNS NULL ON NULL INPUT
    SECURITY DEFINER
    COST 100 ROWS 1000;

    ALTER FUNCTION public.explain_statement(l_query text, out explain json) OWNER TO postgres;
    GRANT EXECUTE ON FUNCTION public.explain_statement(l_query text, out explain json) TO datadog;
EOSQL
