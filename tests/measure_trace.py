import json
import statistics
import time
from pathlib import Path

from markchain.trace import Trace


def main() -> None:
    root = Path(__file__).parent / ".runtime/trace-measurement"
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, object] = {}
    for enabled in (False, True):
        samples = []
        trace = Trace(root / "trace.jsonl" if enabled else None)
        try:
            for _ in range(5):
                started = time.perf_counter()
                for _ in range(1000):
                    with trace.measure("model") as fields:
                        if trace.enabled:
                            fields.update(input_tokens=2000, output_tokens=300)
                samples.append((time.perf_counter() - started) * 1000)
        finally:
            trace.close()
        results[str(enabled)] = {
            "events": 1000,
            "trials": 5,
            "median_ms": statistics.median(samples),
            "samples_ms": samples,
        }
    results["model_calls"] = 0
    results["log_bytes"] = sum(
        path.stat().st_size for path in root.glob("trace.jsonl*")
    )
    print(json.dumps(results))


if __name__ == "__main__":
    main()
