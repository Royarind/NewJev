"""Built-in tools wrapping NewJev capabilities in a clean, declarative registry."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult, ToolRisk
from .registry import CapabilityRegistry, get_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSRF Guard for Web Tools
# ---------------------------------------------------------------------------
BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_safe_url(url: str) -> tuple[bool, str]:
    """Validate that URL is safe to fetch (HTTP/HTTPS, non-internal, non-loopback)."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Malformed URL"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Unsupported scheme '{parsed.scheme}'; only http and https are permitted."

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname in URL"

    if hostname.lower() in ("localhost", "local", "127.0.0.1", "0.0.0.0", "::1"):
        return False, "Access to localhost or loopback addresses is restricted."

    try:
        ip = ipaddress.ip_address(hostname)
        for net in BLOCKED_IP_NETWORKS:
            if ip in net:
                return False, f"Access to private/internal IP {ip} is restricted."
    except ValueError:
        # Hostname is a domain name, not an IP literal
        pass

    return True, ""


# ---------------------------------------------------------------------------
# 1. File and Document Tools
# ---------------------------------------------------------------------------
def _tool_search_files(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..capture.files import find_files, folder_hint

    query = args.get("query", "").strip()
    locations = args.get("locations") or []
    file_type = args.get("file_type")

    roots = locations if locations else None
    hint_dir = folder_hint(query) if not roots else None

    search_name = query
    if file_type and not query.endswith(f".{file_type}"):
        search_name = f"*.{file_type}" if not query else f"*{query}*.{file_type}"

    hits = find_files(search_name, hint_dir=hint_dir, roots=roots, limit=20)
    results = [
        {
            "path": str(p),
            "name": p.name,
            "size": p.stat().st_size if p.exists() else 0,
            "is_dir": p.is_dir() if p.exists() else False,
        }
        for p in hits
    ]
    return ToolResult(
        success=True,
        output=results,
        evidence={"count": len(results), "query": query},
    )


def _tool_read_file(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.fileops import run as file_run

    path = args["path"]
    res = file_run({"action": "read", "src": path})
    return ToolResult(
        success=True,
        output=res,
        evidence={"path": path},
    )


def _tool_write_file(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.fileops import _expand, run as file_run

    path = args["path"]
    content = args.get("content", "")
    overwrite = bool(args.get("overwrite", False))

    target = _expand(path)
    if target.exists() and not overwrite:
        return ToolResult(
            success=False,
            error=f"File '{target}' already exists and overwrite=false (prevented accidental data loss).",
        )

    if target.exists() and overwrite:
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"Overwrote {target} ({len(content)} characters)",
            evidence={"path": str(target), "size": len(content)},
        )

    res = file_run({"action": "write", "src": path, "content": content})
    return ToolResult(
        success=True,
        output=res,
        evidence={"path": str(target), "size": len(content)},
    )


# ---------------------------------------------------------------------------
# 2. PDF Tools
# ---------------------------------------------------------------------------
def _tool_read_pdf(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.pdf import run as pdf_run

    path = args["path"]
    pages = args.get("pages")
    spec = {"action": "read", "src": path}
    if pages:
        spec["pages"] = str(pages)
    res = pdf_run(spec)
    return ToolResult(
        success=True,
        output=res,
        evidence={"path": path},
    )


def _tool_search_pdf(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.pdf import run as pdf_run

    path = args["path"]
    query = args["query"].lower()
    text = pdf_run({"action": "read", "src": path})

    lines = text.splitlines()
    matches = []
    for idx, line in enumerate(lines, 1):
        if query in line.lower():
            start = max(0, idx - 2)
            end = min(len(lines), idx + 2)
            snippet = "\n".join(lines[start:end])
            matches.append({"line": idx, "snippet": snippet})
            if len(matches) >= 10:
                break

    return ToolResult(
        success=True,
        output={"total_matches": len(matches), "matches": matches},
        evidence={"path": path, "query": query, "found": len(matches) > 0},
    )


# ---------------------------------------------------------------------------
# 3. Data and Table Tools
# ---------------------------------------------------------------------------
def _slice_table_range(header: list[str], rows: list[list[Any]], cell_range: str) -> tuple[list[str], list[list[Any]]]:
    """Parse cell range like 'A1:C10', 'A:C', '1:10', 'B2:D5' and slice the table."""
    parts = cell_range.strip().split(":")
    start_str = parts[0].strip()
    end_str = parts[1].strip() if len(parts) > 1 else start_str

    def parse_coord(coord: str) -> tuple[int | None, int | None]:
        m = re.match(r"^([A-Za-z]+)?(\d+)?$", coord)
        if not m:
            return None, None
        col_letters, row_num = m.groups()
        c_idx = None
        if col_letters:
            c_idx = 0
            for ch in col_letters.upper():
                c_idx = c_idx * 26 + ord(ch) - 64
            c_idx -= 1
        r_idx = int(row_num) if row_num else None
        return c_idx, r_idx

    c_start, r_start = parse_coord(start_str)
    c_end, r_end = parse_coord(end_str)

    all_table_rows = [header] + rows
    start_row = max(1, r_start) if r_start is not None else 1
    end_row = min(len(all_table_rows), r_end) if r_end is not None else len(all_table_rows)

    sliced_rows = all_table_rows[start_row - 1 : end_row]
    if not sliced_rows:
        return [], []

    start_col = max(0, c_start) if c_start is not None else 0
    end_col = (c_end + 1) if c_end is not None else len(header)

    sub_header = [str(c) for c in sliced_rows[0][start_col:end_col]]
    sub_body = [r[start_col:end_col] for r in sliced_rows[1:]]
    return sub_header, sub_body


def _tool_read_table(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.data import _resolve, _table, load, run as data_run

    path = args["path"]
    sheet = args.get("sheet")
    cell_range = args.get("range")

    if cell_range:
        try:
            src = _resolve(path)
            header, rows = load(src, sheet)
            sub_header, sub_rows = _slice_table_range(header, rows, cell_range)
            out = f"{src.name} [{cell_range}]: {len(sub_rows)} row(s)\n{_table(sub_header, sub_rows, len(sub_rows))}"
            return ToolResult(
                success=True,
                output=out,
                evidence={"path": path, "range": cell_range, "rows": len(sub_rows)},
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"Could not read range '{cell_range}' from table: {exc}")

    action = args.get("action", "read")
    spec = {"action": action, "src": path}
    if sheet:
        spec["sheet"] = sheet
    if args.get("limit"):
        spec["limit"] = args["limit"]
    try:
        res = data_run(spec)
        return ToolResult(
            success=True,
            output=res,
            evidence={"path": path, "action": action},
        )
    except Exception as exc:
        return ToolResult(success=False, error=f"Data operation failed: {exc}")


def _tool_transform_table(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..shortcuts.data import run as data_run

    spec = {"action": args["operation"], "src": args["path"]}
    if args.get("dst"):
        spec["dst"] = args["dst"]
    if args.get("column"):
        spec["column"] = args["column"]
    if args.get("where"):
        spec["where"] = args["where"]
    if args.get("func"):
        spec["func"] = args["func"]
    if args.get("group_by"):
        spec["group_by"] = args["group_by"]

    res = data_run(spec)
    return ToolResult(
        success=True,
        output=res,
        evidence={"operation": args["operation"], "src": args["path"]},
    )


# ---------------------------------------------------------------------------
# 4. Email Tools
# ---------------------------------------------------------------------------
def _tool_search_email(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts.mail import Mail

    s = get_settings()
    mail = Mail(s.gmail_credentials_path, s.gmail_token_path)
    res = mail.read({"action": "search", "query": args.get("query", ""), "limit": args.get("limit", 10)})
    return ToolResult(
        success=True,
        output=res,
        evidence={"query": args.get("query")},
    )


def _tool_send_email(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts.mail import Mail

    s = get_settings()
    mail = Mail(s.gmail_credentials_path, s.gmail_token_path)
    res = mail.send({
        "to": args["to"],
        "subject": args["subject"],
        "body": args["body"],
        "cc": args.get("cc"),
        "attachments": args.get("attachments"),
    })
    return ToolResult(
        success=True,
        output=res,
        evidence={"to": args["to"], "subject": args["subject"]},
    )


# ---------------------------------------------------------------------------
# 5. Messaging Tools (Slack / Teams)
# ---------------------------------------------------------------------------
def _tool_search_messages(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts import Shortcuts

    sc = Shortcuts(get_settings())
    res = sc.execute("READ_MESSAGES", json.dumps({
        "to": args.get("query") or args.get("channel", ""),
        "limit": args.get("limit", 10),
        "via": args.get("service"),
    }))
    return ToolResult(
        success=True,
        output=res,
        evidence={"service": args.get("service")},
    )


def _tool_send_message(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts import Shortcuts

    sc = Shortcuts(get_settings())
    res = sc.execute("SEND_MESSAGE", json.dumps({
        "to": args["to"],
        "text": args["text"],
        "via": args.get("service"),
    }))
    return ToolResult(
        success=True,
        output=res,
        evidence={"to": args["to"]},
    )


# ---------------------------------------------------------------------------
# 6. Calendar Tools
# ---------------------------------------------------------------------------
def _tool_read_calendar(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts import Shortcuts

    sc = Shortcuts(get_settings())
    res = sc.execute("CALENDAR", json.dumps({
        "action": "list",
        "start": args.get("start"),
        "end": args.get("end"),
    }))
    return ToolResult(
        success=True,
        output=res,
    )


def _tool_create_event(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..config import get_settings
    from ..shortcuts import Shortcuts

    sc = Shortcuts(get_settings())
    res = sc.execute("CALENDAR", json.dumps({
        "action": "create",
        "subject": args["subject"],
        "start": args["start"],
        "end": args.get("end"),
        "attendees": args.get("attendees", []),
        "location": args.get("location", ""),
        "body": args.get("body", ""),
    }))
    return ToolResult(
        success=True,
        output=res,
        evidence={"subject": args["subject"], "start": args["start"]},
    )


# ---------------------------------------------------------------------------
# 7. Browser Tools
# ---------------------------------------------------------------------------
def _tool_browser_open(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    url = args["url"]
    safe, reason = is_safe_url(url)
    if not safe:
        return ToolResult(success=False, error=f"Invalid URL: {reason}")

    # Use context capture if provided, else return explicit unavailable error
    cap = (context or {}).get("browser_capture")
    if cap is None:
        return ToolResult(
            success=False,
            error="No active browser session available in tool context. Browser operations require an active browser context.",
        )

    if hasattr(cap, "navigate"):
        cap.navigate(url)
        page = cap.observe() if hasattr(cap, "observe") else {}
        return ToolResult(
            success=True,
            output={"title": page.get("title", ""), "url": page.get("url", url)},
            evidence={"url": url},
        )
    return ToolResult(
        success=False,
        error="Active browser session does not support navigation.",
    )


def _tool_browser_search(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    query = args["query"]
    search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    return _tool_browser_open({"url": search_url}, context=context)


def _tool_browser_extract(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    cap = (context or {}).get("browser_capture")
    if cap is None:
        return ToolResult(success=False, error="No active browser session in context to extract from.")

    page = cap.observe() if hasattr(cap, "observe") else {}
    full_text = cap.full_text() if hasattr(cap, "full_text") else page.get("text", "")
    query = (args.get("query") or "").lower()

    if query:
        matches = [line.strip() for line in full_text.splitlines() if query in line.lower()]
        output_text = "\n".join(matches[:20])
    else:
        output_text = full_text[:4000]

    return ToolResult(
        success=True,
        output={"url": page.get("url"), "title": page.get("title"), "extracted": output_text},
        evidence={"length": len(output_text)},
    )


# ---------------------------------------------------------------------------
# 8. Semantic Local Search (SQLite FTS over approved files)
# ---------------------------------------------------------------------------
def _tool_semantic_search(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    from ..capture.files import find_files, folder_hint
    from ..agent.memory import get_memory

    query = args["query"].strip()
    locations = args.get("locations") or []
    file_types = args.get("file_types") or ["txt", "md", "csv", "json", "pdf", "py"]
    max_results = min(int(args.get("max_results") or 5), 20)

    matches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # 1. Search persistent approved knowledge index
    try:
        mem = get_memory()
        knowledge_hits = mem.search_knowledge(query, limit=max_results)
        for h in knowledge_hits:
            p_str = h["path"]
            matches.append({
                "path": p_str,
                "filename": h.get("filename") or Path(p_str).name,
                "snippet": h.get("snippet", ""),
                "score": h.get("score", 1.0),
                "source": "knowledge_index",
            })
            seen_paths.add(p_str)
    except Exception as exc:
        logger.debug("Knowledge index query failed: %s", exc)

    # 2. If additional results needed, search candidate files in locations
    remaining_limit = max_results - len(matches)
    if remaining_limit > 0 and (locations or not matches):
        roots = locations if locations else None
        hint_dir = folder_hint(query) if not roots else None

        candidates: list[Path] = []
        for ext in file_types:
            candidates.extend(find_files(f"*.{ext}", hint_dir=hint_dir, roots=roots, limit=50))

        # Filter out already indexed files
        candidates = [p for p in candidates if str(p.resolve()) not in seen_paths and str(p) not in seen_paths]

        if candidates:
            conn = sqlite3.connect(":memory:")
            try:
                cursor = conn.cursor()
                cursor.execute("CREATE VIRTUAL TABLE docs USING fts5(path, filename, content)")

                for p in candidates:
                    try:
                        if p.suffix.lower() == ".pdf":
                            from ..shortcuts.pdf import run as pdf_run
                            text = pdf_run({"action": "read", "src": str(p)})
                        else:
                            text = p.read_text(encoding="utf-8", errors="replace")[:100_000]
                        cursor.execute("INSERT INTO docs(path, filename, content) VALUES(?, ?, ?)", (str(p), p.name, text))
                    except Exception:
                        continue

                clean_q = re.sub(r"[^\w\s]", " ", query).strip() or query
                fts_query = " OR ".join(f'"{token}"' for token in clean_q.split() if len(token) > 2)
                if fts_query:
                    cursor.execute(
                        """
                        SELECT path, filename, snippet(docs, 2, '>>>', '<<<', '...', 25), rank
                        FROM docs WHERE docs MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, remaining_limit),
                    )
                    for row in cursor.fetchall():
                        matches.append({
                            "path": row[0],
                            "filename": row[1],
                            "snippet": row[2],
                            "score": round(float(row[3]), 3),
                            "source": "local_files",
                        })
            except Exception as exc:
                logger.debug("Local candidate FTS search failed: %s", exc)
            finally:
                conn.close()

    return ToolResult(
        success=True,
        output={"query": query, "matches": matches},
        evidence={"count": len(matches), "query": query},
    )


# ---------------------------------------------------------------------------
# 9. Web Extraction (Safe HTTP Retrieval)
# ---------------------------------------------------------------------------
def _tool_extract_web_content(args: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
    import httpx

    url = args["url"].strip()
    query = (args.get("query") or "").strip().lower()

    safe, reason = is_safe_url(url)
    if not safe:
        return ToolResult(success=False, error=f"URL validation failed: {reason}")

    current_url = url
    max_redirects = 5
    html_text = ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            for _ in range(max_redirects):
                safe, reason = is_safe_url(current_url)
                if not safe:
                    return ToolResult(
                        success=False,
                        error=f"URL validation failed during redirect to '{current_url}': {reason}",
                    )
                resp = client.get(current_url, headers=headers)
                if resp.is_redirect:
                    loc = resp.headers.get("location")
                    if not loc:
                        return ToolResult(
                            success=False,
                            error=f"Redirect from '{current_url}' missing Location header.",
                        )
                    current_url = urllib.parse.urljoin(current_url, loc)
                else:
                    resp.raise_for_status()
                    html_text = resp.text
                    break
            else:
                return ToolResult(
                    success=False,
                    error=f"Too many redirects (exceeded limit of {max_redirects}).",
                )
    except Exception as exc:
        return ToolResult(success=False, error=f"HTTP request failed: {exc}")

    # Strip script/style tags and extract readable text
    clean = re.sub(r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>", " ", html_text, flags=re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Filter by query if requested
    if query:
        sentences = [s.strip() for s in re.split(r"[.!?\n]", clean) if query in s.lower()]
        extracted = ". ".join(sentences[:10])
    else:
        extracted = clean[:5000]

    return ToolResult(
        success=True,
        output={
            "url": url,
            "title": (re.search(r"<title[^>]*>([^<]+)</title>", html_text, re.I) or ["", ""])[1].strip(),
            "content": extracted,
        },
        evidence={"url": url, "length": len(extracted)},
    )


# ---------------------------------------------------------------------------
# Tool Declarations and Registration
# ---------------------------------------------------------------------------
def register_builtin_tools(registry: CapabilityRegistry | None = None) -> None:
    reg = registry or get_registry()

    # 1. search_files
    reg.register(Tool(
        name="search_files",
        description="Find files and folders by name, pattern, or type on the user's computer.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "File name, wildcard pattern, or search words"},
                "locations": {"type": "array", "description": "Specific folder paths to search within"},
                "file_type": {"type": "string", "description": "Extension filter without dot (e.g. 'pdf', 'csv', 'docx')"},
            },
            "required": ["query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="file_list_check",
        handler=_tool_search_files,
    ))

    # 2. read_file
    reg.register(Tool(
        name="read_file",
        description="Read the text contents of a local file (e.g. .txt, .md, .csv, .json, .py).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path or known file name to read"},
            },
            "required": ["path"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="file_read_check",
        handler=_tool_read_file,
    ))

    # 3. write_file
    reg.register(Tool(
        name="write_file",
        description="Write text to a file on disk. Protects against overwriting unless overwrite=true.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Destination file path"},
                "content": {"type": "string", "description": "Text content to write"},
                "overwrite": {"type": "boolean", "description": "Explicitly allow replacing existing file"},
            },
            "required": ["path", "content"],
        },
        risk_level=ToolRisk.SAFE_WRITE,
        side_effects=["Modifies file on local disk"],
        verification_method="file_exists_and_content_check",
        handler=_tool_write_file,
    ))

    # 4. read_pdf
    reg.register(Tool(
        name="read_pdf",
        description="Extract readable text and metadata from a PDF file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to PDF file"},
                "pages": {"type": "string", "description": "Optional page range, e.g. '1-3'"},
            },
            "required": ["path"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="text_extracted_check",
        handler=_tool_read_pdf,
    ))

    # 5. search_pdf
    reg.register(Tool(
        name="search_pdf",
        description="Search for specific keywords, figures, or phrases inside a PDF file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to PDF file"},
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
            },
            "required": ["path", "query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="keyword_matches_check",
        handler=_tool_search_pdf,
    ))

    # 6. read_table
    reg.register(Tool(
        name="read_table",
        description="Read headers, rows, or summary statistics from a CSV or Excel spreadsheet.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to CSV or Excel file"},
                "sheet": {"type": "string", "description": "Sheet name if Excel"},
                "range": {"type": "string", "description": "Cell range, e.g. 'A1:C20'"},
            },
            "required": ["path"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="table_structure_check",
        handler=_tool_read_table,
    ))

    # 7. transform_table
    reg.register(Tool(
        name="transform_table",
        description="Perform operations (sort, filter, aggregate, convert) on a tabular CSV/Excel file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Source file path"},
                "operation": {"type": "string", "enum": ["sort", "filter", "aggregate", "append_rows", "convert"], "description": "Operation type"},
                "dst": {"type": "string", "description": "Target file path for transformed result"},
                "column": {"type": "string", "description": "Target column for sort/aggregate"},
                "where": {"type": "array", "description": "Filter criteria list"},
                "func": {"type": "string", "description": "Aggregation function: sum|avg|min|max|count"},
                "group_by": {"type": "string", "description": "Grouping column"},
            },
            "required": ["path", "operation"],
        },
        risk_level=ToolRisk.SAFE_WRITE,
        side_effects=["Creates transformed output table file"],
        verification_method="destination_table_check",
        handler=_tool_transform_table,
    ))

    # 8. search_email
    reg.register(Tool(
        name="search_email",
        description="Search recent emails from Gmail or Outlook by keyword, sender, or date.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Email search query"},
                "folder": {"type": "string", "description": "Mailbox folder, e.g. 'INBOX'"},
                "limit": {"type": "integer", "description": "Maximum emails to return"},
            },
            "required": ["query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="email_results_check",
        handler=_tool_search_email,
    ))

    # 9. send_email
    reg.register(Tool(
        name="send_email",
        description="Send an email to one or more recipients. Requires explicit confirmation.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es)"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Email body text"},
                "cc": {"type": "string", "description": "CC recipient(es)"},
                "attachments": {"type": "array", "description": "List of file paths to attach"},
            },
            "required": ["to", "subject", "body"],
        },
        risk_level=ToolRisk.EXTERNAL_COMMUNICATION,
        side_effects=["Sends email over internet to recipients"],
        verification_method="send_receipt_check",
        handler=_tool_send_email,
    ))

    # 10. search_messages
    reg.register(Tool(
        name="search_messages",
        description="Search chat messages from Slack or Microsoft Teams.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword or user/channel name"},
                "service": {"type": "string", "enum": ["slack", "teams"], "description": "Chat platform"},
                "limit": {"type": "integer", "description": "Max messages to return"},
            },
            "required": ["query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="message_list_check",
        handler=_tool_search_messages,
    ))

    # 11. send_message
    reg.register(Tool(
        name="send_message",
        description="Send a message to a Slack channel/user or Teams chat. Requires explicit confirmation.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Channel name (e.g. '#general') or user email/id"},
                "text": {"type": "string", "description": "Message text"},
                "service": {"type": "string", "enum": ["slack", "teams"], "description": "Chat platform"},
            },
            "required": ["to", "text"],
        },
        risk_level=ToolRisk.EXTERNAL_COMMUNICATION,
        side_effects=["Sends real chat message"],
        verification_method="chat_delivery_check",
        handler=_tool_send_message,
    ))

    # 12. read_calendar
    reg.register(Tool(
        name="read_calendar",
        description="Read calendar events and upcoming meetings from Outlook or Microsoft 365.",
        parameters={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start date/time (YYYY-MM-DD)"},
                "end": {"type": "string", "description": "End date/time (YYYY-MM-DD)"},
            },
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="event_list_check",
        handler=_tool_read_calendar,
    ))

    # 13. create_event
    reg.register(Tool(
        name="create_event",
        description="Create a calendar meeting or appointment. Requires confirmation if inviting attendees.",
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Title/subject of meeting"},
                "start": {"type": "string", "description": "Start date/time (YYYY-MM-DD HH:MM)"},
                "end": {"type": "string", "description": "End date/time (YYYY-MM-DD HH:MM)"},
                "attendees": {"type": "array", "description": "List of attendee email addresses"},
                "location": {"type": "string", "description": "Meeting room or location"},
                "body": {"type": "string", "description": "Agenda or description"},
            },
            "required": ["subject", "start"],
        },
        risk_level=ToolRisk.EXTERNAL_COMMUNICATION,
        side_effects=["Schedules event on calendar and sends invites"],
        verification_method="calendar_event_check",
        handler=_tool_create_event,
    ))

    # 14. browser_open
    reg.register(Tool(
        name="browser_open",
        description="Open a validated web URL in the automated browser.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTP/HTTPS URL"},
            },
            "required": ["url"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="browser_url_check",
        handler=_tool_browser_open,
    ))

    # 15. browser_search
    reg.register(Tool(
        name="browser_search",
        description="Execute a web search query in the automated browser.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term"},
            },
            "required": ["query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="browser_search_results_check",
        handler=_tool_browser_search,
    ))

    # 16. browser_extract
    reg.register(Tool(
        name="browser_extract",
        description="Extract structured text or answers from the currently loaded browser page.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword filter or specific piece of information to locate"},
            },
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="extracted_text_check",
        handler=_tool_browser_extract,
    ))

    # 17. semantic_search
    reg.register(Tool(
        name="semantic_search",
        description="Search user-approved local files by meaning or keywords using local passage ranking.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Concepts, keywords, or question"},
                "locations": {"type": "array", "description": "Folders to search (defaults to user documents/desktop)"},
                "file_types": {"type": "array", "description": "File extensions to include"},
                "max_results": {"type": "integer", "description": "Maximum relevant passages to return"},
            },
            "required": ["query"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="passage_match_check",
        handler=_tool_semantic_search,
    ))

    # 18. extract_web_content
    reg.register(Tool(
        name="extract_web_content",
        description="Directly fetch and extract structured readable content from a public web page.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public web URL to extract from"},
                "query": {"type": "string", "description": "Optional topic or filter keyword"},
            },
            "required": ["url"],
        },
        risk_level=ToolRisk.READ_ONLY,
        verification_method="content_payload_check",
        handler=_tool_extract_web_content,
    ))


# Initialize registry on import
register_builtin_tools(get_registry())
