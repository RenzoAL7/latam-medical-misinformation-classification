#!/usr/bin/env python3
"""Descarga URLs Bronze y extrae bodies localmente con BeautifulSoup."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.collection import extract_body_from_html
from claim_extraction.data import read_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV/JSONL Bronze con canonical_url.")
    parser.add_argument("--output", required=True, help="CSV local con body y estado de extracción.")
    parser.add_argument("--min-body-chars", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-text-chars", type=int, default=200_000)
    parser.add_argument("--user-agent", default="mednews-local/1.0 (+research body extraction)")
    return parser.parse_args()


def download_html(url: str, *, timeout: float, max_bytes: int, user_agent: str) -> tuple[str, int]:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "es-419,es;q=0.9,en;q=0.5",
            "User-Agent": user_agent,
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)[:max_bytes]
        content_type = str(response.headers.get("Content-Type", ""))
        charset = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
        encoding = charset.group(1) if charset else "utf-8"
        try:
            html = raw.decode(encoding, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return html, int(response.status or 200)


def extract_row(row: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    output = dict(row)
    output.update(
        {
            "body": "",
            "body_available": "NO",
            "http_status": "",
            "extraction_method": "none",
            "extraction_status": "ERROR",
            "body_char_count": 0,
            "body_word_count": 0,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extraction_error": "",
        }
    )
    url = str(row.get("canonical_url") or row.get("url") or row.get("link") or "").strip()
    if not url.startswith(("http://", "https://")):
        output.update({"extraction_status": "URL_ERROR", "extraction_error": "URL http(s) ausente o inválida"})
        return output
    try:
        html, http_status = download_html(url, timeout=args.timeout, max_bytes=args.max_bytes, user_agent=args.user_agent)
        body, method = extract_body_from_html(html)
        body = body[: args.max_text_chars]
        words = len(re.findall(r"\b[\wÀ-ÿ]+\b", body, flags=re.UNICODE))
        output.update(
            {
                "body": body,
                "http_status": http_status,
                "extraction_method": method,
                "body_char_count": len(body),
                "body_word_count": words,
                "body_available": "SI" if len(body) >= args.min_body_chars else "NO",
                "extraction_status": "OK" if len(body) >= args.min_body_chars else "CUERPO_INSUFICIENTE",
            }
        )
    except HTTPError as exc:
        output.update({"http_status": exc.code, "extraction_status": "HTTP_ERROR", "extraction_error": f"HTTP {exc.code}"})
    except (URLError, TimeoutError, OSError) as exc:
        output.update({"extraction_status": "URL_ERROR", "extraction_error": str(exc)[:500]})
    except Exception as exc:  # noqa: BLE001 - conserva la fila y el diagnóstico.
        output.update({"extraction_status": "ERROR", "extraction_error": str(exc)[:500]})
    return output


def main() -> None:
    args = parse_args()
    if args.min_body_chars < 1 or args.timeout <= 0 or args.delay < 0 or args.max_bytes < 1 or args.max_text_chars < 1:
        raise ValueError("Los límites de extracción deben ser positivos; delay puede ser cero.")
    frame = read_table(Path(args.input).expanduser())
    if "record_id" not in frame or frame["record_id"].duplicated().any():
        raise ValueError("El inventario Bronze debe tener record_id único.")
    if not any(column in frame for column in ("canonical_url", "url", "link")):
        raise ValueError("El inventario Bronze debe contener canonical_url, url o link.")
    rows = []
    for index, row in enumerate(frame.to_dict(orient="records")):
        if index and args.delay:
            time.sleep(args.delay)
        rows.append(extract_row(row, args))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = pd.DataFrame(rows)
    result.to_csv(output, index=False, encoding="utf-8")
    print(json.dumps({"input_rows": len(frame), "ok_rows": int((result["extraction_status"] == "OK").sum()), "output": str(output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
