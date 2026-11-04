#!/bin/bash
# Stands in for `vivado` in tests: accepts the flags the launcher passes and
# runs the plugin on the tclsh in $VRC_TEST_TCLSH, so the launch path is exercised
# end to end (setsid, log redirection, readiness poll, killpg) with no Vivado.
plugin=""
while [ $# -gt 0 ]; do
    case "$1" in
        -source) plugin="$2"; shift 2 ;;
        -mode)   shift 2 ;;
        -nolog|-nojournal|-notrace) shift ;;
        *) shift ;;
    esac
done
if [ -z "$plugin" ]; then
    echo "fake_vivado: no -source given" >&2
    exit 64
fi
# A `version -short` from the CLI should look like Vivado. The preamble is
# followed by our own stdin, so tclsh goes on to read the `::vrc::serve`
# stream `vrc start` pipes in, as Vivado's prompt does.
{
    printf 'proc version {args} { return "2023.2.2 (fake)" }\n'
    printf 'source %s\n' "$plugin"
    cat
} | exec "$VRC_TEST_TCLSH"
