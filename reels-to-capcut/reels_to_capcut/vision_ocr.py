#!/usr/bin/env /usr/bin/python3
"""Small macOS Vision bridge, intentionally run with Apple's system Python."""

from __future__ import annotations

import json
import sys


def recognize(path: str) -> list[str]:
    from Foundation import NSURL
    from Quartz import CGImageSourceCreateImageAtIndex, CGImageSourceCreateWithURL
    import Vision

    source = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    image = CGImageSourceCreateImageAtIndex(source, 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setRecognitionLanguages_(["ko-KR", "en-US"])
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(str(error))
    found: list[tuple[float, float, str]] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        box = observation.boundingBox()
        found.append((-float(box.origin.y), float(box.origin.x), str(candidates[0].string())))
    return [text for _, _, text in sorted(found) if text.strip()]


if __name__ == "__main__":
    try:
        print(json.dumps({"lines": recognize(sys.argv[1])}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
