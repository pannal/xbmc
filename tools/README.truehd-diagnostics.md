# TrueHD tracing and regression checks

The final-build cleanup removes the dedicated MAT branch/emit/dequeue and codec
timing records from `dfe303ff034be78f425a615336b60babbcf43478`, and the
codec/sink/ALSA delivery CRC records from
`6218b2ecb9144eada9384f052223df1f4e5c02b8`. Earlier operational, error and recovery
logging remains. This cleanup does not claim to fix the reported dropouts or
provide effective dialnorm defeat for Evolution streams.

The complete instrumented source is preserved locally at
`codex/truehd-delivery-diagnostics`, initially
`9a90ccab8904ef1cf98b18bc7d1348cf469f33f2`. The cleanup is prepared separately on
`codex/truehd-final-cleanup`. Neither branch changes the public-test checkout or
the CE build input automatically.

Run these from the source checkout being verified:

```sh
python3 tools/test-truehd-mat-diagnostics.py
python3 tools/test-truehd-delivery.py
python3 tools/check-truehd-delivery.py --self-test
python3 tools/test-activeae-truehd-sync.py --negative-control
python3 tools/test-truehd-evolution-dialnorm.py --baseline-ref 6218b2ecb9144eada9384f052223df1f4e5c02b8
```

The MAT and delivery tests default both `--baseline-ref` and `--diagnostic-ref`
to the full pre-cleanup hash above. They compare the working production source
against that baseline, then require the instrumented source's diagnostic
assertions and negative controls to pass. For explicit local branch selection:

```sh
python3 tools/test-truehd-mat-diagnostics.py --diagnostic-ref codex/truehd-delivery-diagnostics
python3 tools/test-truehd-delivery.py --diagnostic-ref codex/truehd-delivery-diagnostics
```

A selected diagnostic revision without the required tracing fails; controls are
never skipped merely because the working source is cleaned. The MAT padding
and ALSA offset negative controls always exercise the working production source.
All original functional assertions remain. A later functional source change may
require a deliberate baseline update after reviewing the difference.

Both tools accept `--output` to retain generated fixtures and `--sanitize` for
ASan/UBSan. A host that prevents LeakSanitizer from inspecting threads needs
`ASAN_OPTIONS=detect_leaks=0`; record this limit rather than claiming a leak
check. The MAT fixture already disables leak checking for this reason. The parser
test accepts `FFMPEG_INCLUDE` for the libavutil public headers and repeated
`--media` arguments for complete raw TrueHD streams.

`test-truehd-delivery.py --media /absolute/path/sample.thd --catalog catalog.json`
still generates source-specific MAT/carrier catalogs from cleaned production.
`check-truehd-delivery.py` remains a captured-log analysis tool: it needs logs
from an instrumented build, even though the generator can run against cleaned
source. These host adapters and accepted-write checks do not establish physical
HDMI/eARC audio or timing neutrality.

Integrate both cleanup-branch commits, in order, into the intended final-build
source only after checking its current base and unrelated changes. Keep the
public-test source instrumented while the investigation needs it. To restore
tracing on a branch containing the cleanup, revert only the production tracing
removal commit; keep the test/source-selection commit. Alternatively use the
preserved diagnostic branch in a separate checkout. Inspect conflicts and rerun
the checks after later source changes. Panni handles image builds and release
integration; no build or device step is part of this cleanup.
