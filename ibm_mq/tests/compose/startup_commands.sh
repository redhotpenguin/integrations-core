set -e

cat << EOF | runmqsc datadog
ALTER QMGR ACCTINT(1) ACCTMQI(ON) ACCTQ(ON)
ALTER QMGR STATINT(1) STATMQI(ON) STATQ(ON) STATCHL(MEDIUM) STATACLS(MEDIUM)
EOF
