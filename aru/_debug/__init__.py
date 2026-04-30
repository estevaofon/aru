"""Debug instrumentation package — gated by env vars, zero cost when off.

See ``loop_tracer.py`` for the Ctrl+C / loop-saturation tracer
(``ARU_DEBUG_LOOP=1``). New tracers go here as siblings; each owns its
own env var and log file under ``~/.aru/``.
"""
