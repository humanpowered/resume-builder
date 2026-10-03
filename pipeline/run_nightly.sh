#!/bin/sh
# Nightly job-search run, for cron on macOS or Linux.
#
# Everything it does lives in src/run_nightly.py -- the logging, the step
# order, the verdict line and the log pruning. This file only finds Python and
# hands over.
#
# Run it by hand any time to test:  ./run_nightly.sh
#
# cron runs with a minimal PATH, so if it fails to find Python, set
# JOB_PIPELINE_PYTHON to the full path of your interpreter.

cd "$(dirname "$0")/src" || exit 1
exec "${JOB_PIPELINE_PYTHON:-python3}" run_nightly.py "$@"
