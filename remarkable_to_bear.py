#!/usr/bin/env python3
"""
remarkable-to-bear: Convert reMarkable handwritten notes to Bear notes via GPT-4o Vision.

Pulls your "Quick Sheets" notebook from the reMarkable cloud (or accepts a local PDF),
sends each page to OpenAI's vision model, and creates structured Bear notes + Apple
Reminders from the recognized handwriting.

Configuration: copy .env.example to .env and fill in your values.
Prompt editing: modify prompt.txt to change how the AI interprets your notes.
"""

# Raycast metadata (harmless when run outside Raycast)
# @raycast.schemaVersion 1
# @raycast.title reMarkable → Bear
# @raycast.mode fullOutput
# @raycast.icon 📝
# @raycast.description Sends reMarkable notes to GPT-4o Vision → Bear notes + Reminders
# @raycast.packageName Remarkable
# @raycast.argument1 { "type": "file", "placeholder": "Select a PDF (optional)" }

import os
import sys
import json
import time
import base64
import subprocess
import tempfile
import shutil
import urllib.request
import urllib.parse
import urllib.error
import traceback
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

logging.getLogger("pdfrw").setLevel(logging.CRITICAL)
logging.disable(logging.WARNING)

# -------------------------
# Resolve script directory (for .env and prompt.txt)
# -------------------------
SCRIPT_DIR = Path(__file__).resolve().parent


# -------------------------
# .env loader (no external dependency)
# -------------------------
def _load_dotenv(path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ (does not overwrite)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(SCRIPT_DIR / ".env")

# -------------------------
# PATH fix (Raycast and other launchers may have a minimal PATH)
# -------------------------
os.environ["PATH"] = (
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
    + os.environ.get("PATH", "")
)

# -------------------------
# Configuration (all from environment / .env)
# -------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

CREATE_REMINDERS = os.environ.get("CREATE_REMINDERS", "true").lower() in ("true", "1", "yes")
TODO_APP = os.environ.get("TODO_APP", "reminders").lower()  # "reminders", "things", or "both"
DEFAULT_REMINDERS_LIST = os.environ.get("DEFAULT_REMINDERS_LIST", "Work")
DEFAULT_THINGS_LIST = os.environ.get("DEFAULT_THINGS_LIST", "")

try:
    MAX_PAGES = int(os.environ.get("MAX_PAGES", "12"))
except ValueError:
    MAX_PAGES = 12
try:
    IMAGE_DPI = int(os.environ.get("IMAGE_DPI", "170"))
except ValueError:
    IMAGE_DPI = 170
MAX_BASE64_BYTES = 24 * 1024 * 1024

RMAPI_SEARCH_TERM = os.environ.get("RMAPI_SEARCH_TERM", "Quick sheets")
DELETE_AFTER_PROCESSING = os.environ.get("DELETE_AFTER_PROCESSING", "true").lower() in ("true", "1", "yes")

PRINT_MODEL_RAW_OUTPUT = os.environ.get("DEBUG_RAW_OUTPUT", "false").lower() in ("true", "1", "yes")


# -------------------------
# Prompt loader
# -------------------------
def _load_prompt() -> str:
    """Load the system prompt from prompt.txt next to this script."""
    prompt_file = SCRIPT_DIR / "prompt.txt"
    if prompt_file.is_file():
        return prompt_file.read_text(encoding="utf-8").strip()
    print("⚠️  prompt.txt not found next to script — using built-in default prompt.")
    return _DEFAULT_PROMPT


_DEFAULT_PROMPT = """You are reading handwritten notes exported from a reMarkable tablet as images.

Return ONLY valid JSON. Do NOT include markdown fences or commentary.
NEVER return an empty notes array.

Schema:
{
  "notes": [
    {
      "title": "Subject - (YYYY-MM-DD)",
      "tags": ["tag1", "tag2", "tag3"],
      "body": "Markdown body for Bear (NO action items section)",
      "action_items": [
        { "title": "Do something", "due_date": "YYYY-MM-DD or null", "list": "Work or null" }
      ]
    }
  ]
}

Rules:
- Return at least one note.
- A horizontal line or underline is a section separator WITHIN a note, NOT a boundary between notes.
- Split into multiple notes ONLY for clearly separate, unrelated meetings or topics.
- If the page contains one topic (even with multiple sections), return exactly ONE note.
- Titles should be concise.
- "tags": 3-4 meaningful tags from the content (e.g. topic, project, team). No leading #. Nested tags allowed like "ops/team". No generic tags like "notes" or "remarkable".
- "body": Bear-friendly Markdown. MUST NOT include Action Items.
- The FIRST line of body MUST be hashtags (e.g. "#hubspot #sync #crm"). Do NOT repeat them.
- Body structure:
  ## Agenda
  ## Decisions (optional)
  ## Notes - Key Points
  ## Open Questions (optional)

Action items:
- Include every action item from the notes.
- Each: "title" required. "due_date" optional (null if unknown). "list" optional (null if unknown).

Accuracy:
- Preserve meaning; do not invent content.
- Read handwriting carefully.
"""

SYSTEM_PROMPT = _load_prompt()


# -------------------------
# Utilities
# -------------------------
def _applescript_escape(s: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run(cmd: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)


def choose_pdf() -> str:
    cp = run([
        "osascript", "-e",
        'POSIX path of (choose file with prompt "Select a reMarkable PDF")'
    ])
    if cp.returncode != 0:
        raise RuntimeError("File picker cancelled")
    return cp.stdout.strip()


def rmapi_run(args: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        cp = run(["rmapi"] + args, cwd=cwd)
    except FileNotFoundError:
        raise RuntimeError(
            "rmapi not found. Install it: https://github.com/ddvber/rmapi\n"
            "  brew install rmapi  (or)  go install github.com/ddvber/rmapi@latest"
        )
    if cp.returncode != 0:
        raise RuntimeError(f"rmapi failed: {' '.join(args)}\n{(cp.stderr or cp.stdout).strip()}")
    return cp


def parse_rmapi_find_output(output: str) -> List[str]:
    paths: List[str] = []
    for line in (output or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\[[fd]\]\s+(.*)$", s)
        if m:
            paths.append(m.group(1).strip())
            continue
        if s.startswith("/"):
            paths.append(s)
    return paths


def render_rmdoc_to_pdf(zip_path: Path, tmp: str) -> Path:
    """Render a reMarkable notebook zip/rmdoc to PDF using rmc (supports v6 format)."""
    import zipfile
    import json as _json

    try:
        from rmc import rm_to_svg
        from rmc.exporters import writing_tools as _wt
        import cairosvg
        from pdfrw import PdfReader, PdfWriter
    except ImportError:
        raise RuntimeError(
            "Cannot render reMarkable notebook: missing optional dependencies.\n"
            "Run: pip install cairosvg pdfrw 'rmc @ git+https://github.com/ricklupton/rmc.git'"
        )

    # Patch rmc's palette with all reMarkable colors (including highlight and newer colors)
    # rmc intentionally skips HIGHLIGHT (9) which causes KeyError on pages with highlighter
    _missing_colors = {
        9: (251, 247, 25),    # HIGHLIGHT — default yellow highlighter
    }
    for color_id, rgb in _missing_colors.items():
        if color_id not in _wt.RM_PALETTE:
            _wt.RM_PALETTE[color_id] = rgb

    extract_dir = Path(tmp) / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (extract_dir / member).resolve()
            if not str(target).startswith(str(extract_dir.resolve())):
                raise RuntimeError(f"Zip path traversal detected: {member}")
        zf.extractall(extract_dir)

    content_files = list(extract_dir.glob("*.content"))
    if not content_files:
        raise RuntimeError("No .content file found in notebook zip")
    with open(content_files[0]) as f:
        content = _json.load(f)
    if "cPages" in content:
        raw = content["cPages"].get("pages", [])
        page_ids = [p["id"] if isinstance(p, dict) else p for p in raw]
    else:
        page_ids = content.get("pages", [])
    if not page_ids:
        raise RuntimeError("No pages found in notebook content file")

    rm_dirs = [d for d in extract_dir.iterdir() if d.is_dir()]
    if not rm_dirs:
        raise RuntimeError("No page directory found in notebook zip")
    rm_dir = rm_dirs[0]

    writer = PdfWriter()
    pages_written = 0
    for i, pid in enumerate(page_ids):
        rm_file = rm_dir / f"{pid}.rm"
        if not rm_file.exists():
            continue
        svg_file = Path(tmp) / f"page-{i:03d}.svg"
        page_pdf = Path(tmp) / f"page-{i:03d}.pdf"
        try:
            rm_to_svg(rm_file, svg_file)
            cairosvg.svg2pdf(url=str(svg_file), write_to=str(page_pdf))
        except Exception as e:
            import traceback as _tb
            print(f"  ⚠️ Could not render page {i+1}: {type(e).__name__}: {e}")
            _tb.print_exc()
            continue
        reader = PdfReader(str(page_pdf))
        for page in reader.pages:
            writer.addpage(page)
        pages_written += 1

    if pages_written == 0:
        raise RuntimeError("rmc could not render any pages from the notebook")

    out_pdf = Path(tmp) / "notebook.pdf"
    writer.write(str(out_pdf))
    return out_pdf


def get_notebook_pdf_from_cloud() -> tuple[Optional[str], List[Path], str, str]:
    """
    Pull notebook from reMarkable cloud via rmapi.
    Returns (pdf_path_or_None, direct_images, cloud_path, temp_dir).
    """
    cp = rmapi_run(["find", ".", f"(?i){re.escape(RMAPI_SEARCH_TERM)}"])
    matches = parse_rmapi_find_output(cp.stdout)
    seen = set()
    filtered = []
    for m in matches:
        if m not in seen and not m.lower().startswith("/trash/"):
            seen.add(m)
            filtered.append(m)
    matches = filtered
    if not matches:
        raise RuntimeError(f"No '{RMAPI_SEARCH_TERM}' found in reMarkable cloud.")
    if len(matches) > 1:
        raise RuntimeError("Multiple matches found:\n" + "\n".join(matches))

    cloud_path = matches[0]
    tmp = tempfile.mkdtemp(prefix="rm_cloud_")

    try:
        rmapi_run(["geta", cloud_path], cwd=tmp)
    except RuntimeError:
        rmapi_run(["get", cloud_path], cwd=tmp)

    all_files = sorted(Path(tmp).rglob("*"))
    pdfs = [f for f in all_files if f.suffix.lower() == ".pdf"]
    if pdfs:
        return str(pdfs[0]), [], cloud_path, tmp

    zips = sorted(
        (f for f in all_files if f.suffix.lower() in (".zip", ".rmdoc")),
        key=lambda f: (0 if f.suffix.lower() == ".zip" else 1)
    )
    if not zips:
        file_list = "\n".join(str(f) for f in all_files) or "(none)"
        raise RuntimeError(f"rmapi did not produce a PDF or zip. Files:\n{file_list}")
    pdf_path = render_rmdoc_to_pdf(zips[0], tmp)
    return str(pdf_path), [], cloud_path, tmp


def pdf_to_images(pdf_path: str) -> tuple[List[Path], str]:
    """Convert PDF pages to PNG images. Returns (images, temp_dir) — caller must clean up temp_dir."""
    if run(["/usr/bin/which", "pdftoppm"]).returncode != 0:
        raise RuntimeError("pdftoppm not found. Install with: brew install poppler")

    tmp = tempfile.mkdtemp(prefix="rm_pages_")
    out = os.path.join(tmp, "page")

    cp = run(["pdftoppm", "-png", "-r", str(IMAGE_DPI), pdf_path, out])
    if cp.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("pdftoppm failed:\n" + (cp.stderr or cp.stdout))

    images = sorted(Path(tmp).glob("page-*.png"))
    if not images:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("No images produced from PDF")
    if len(images) > MAX_PAGES:
        images = images[:MAX_PAGES]
    return images, tmp


def encode_image_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def estimate_base64_bytes(images: List[Path]) -> int:
    total = 0
    for p in images:
        n = p.stat().st_size
        total += 4 * ((n + 2) // 3)
    return total


def call_openai_chat_completions(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set.\n"
            "Set it in your .env file or export it as an environment variable.\n"
            "Get your key at: https://platform.openai.com/api-keys"
        )

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        body = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***REDACTED***", body)
        raise RuntimeError(f"OpenAI HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI connection error: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse OpenAI JSON response: {e}")


def openai_vision(images: List[Path]) -> str:
    est_b64 = estimate_base64_bytes(images)
    if est_b64 > MAX_BASE64_BYTES:
        raise RuntimeError(
            f"Payload too large (~{est_b64/1024/1024:.1f} MB base64). "
            "Lower IMAGE_DPI or MAX_PAGES in your .env."
        )

    content: List[Dict[str, Any]] = [{"type": "text", "text": "Read these handwritten notes and produce the JSON response."}]
    for img in images:
        b64 = encode_image_b64(img)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"}
        })

    payload = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
    }

    j = call_openai_chat_completions(payload)
    msg = j["choices"][0]["message"].get("content", "")
    return msg or ""


def notify(title: str, message: str) -> None:
    msg_esc = _applescript_escape(message)
    title_esc = _applescript_escape(title)
    run(["osascript", "-e", f'display notification "{msg_esc}" with title "{title_esc}"'])


def sanitize_title(s: str) -> str:
    return " ".join((s or "").split())[:120] or "reMarkable Notes"


def normalize_tags(tags) -> List[str]:
    out: List[str] = []
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if " " in t:
            t = t.replace(" ", "-")
        if "," in t:
            t = t.replace(",", "-")
        if t and t not in out:
            out.append(t)
    return out


def tags_line(tags: List[str]) -> str:
    return " ".join("#" + t for t in tags) if tags else ""


def strip_code_fences(s: str) -> str:
    s2 = (s or "").strip()
    if s2.startswith("```"):
        lines = s2.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return s2


def remove_action_items_section(markdown: str) -> str:
    """Strip any Action Items section the model may have included despite instructions."""
    if not markdown:
        return markdown
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    pattern = re.compile(r"(?im)^(#{2,6})\s*action items\s*$")
    while True:
        m = pattern.search(text)
        if not m:
            return text
        start = m.start()
        level = len(m.group(1))
        next_heading = re.compile(rf"(?m)^#{{1,{level}}}\s+\S")
        m2 = next_heading.search(text, m.end())
        end = m2.start() if m2 else len(text)
        text = (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip() + "\n"


# -------------------------
# Bear
# -------------------------
def bear_create(title: str, body: str, open_note: bool = False, images: Optional[List[Path]] = None) -> None:
    create_url = "bear://x-callback-url/create?" + urllib.parse.urlencode({
        "title": title,
        "text": body,
        "open_note": "yes" if open_note else "no",
    }, quote_via=urllib.parse.quote)

    if not images:
        run(["open", create_url])
        return

    script_lines = [f'open location "{create_url}"', "delay 1.5"]
    for i, img in enumerate(images):
        b64 = base64.b64encode(img.read_bytes()).decode("utf-8")
        add_url = "bear://x-callback-url/add-file?" + urllib.parse.urlencode({
            "title": title,
            "file": b64,
            "filename": f"page-{i + 1}.png",
            "mode": "append",
        }, quote_via=urllib.parse.quote)
        script_lines.append(f'open location "{add_url}"')
        if i < len(images) - 1:
            script_lines.append("delay 0.3")

    script = "\n".join(script_lines)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        run(["osascript", script_path])
    finally:
        os.unlink(script_path)


# -------------------------
# Reminders
# -------------------------
def create_reminder(title: str, list_name: str = "", due_date: Optional[str] = None, note_title: str = "") -> tuple[bool, str]:
    list_name = list_name or DEFAULT_REMINDERS_LIST
    title_esc = _applescript_escape(title or "")
    list_esc = _applescript_escape(list_name)
    body_esc = _applescript_escape(f"From: {note_title}" if note_title else "")
    body_prop = f', body:"{body_esc}"' if body_esc else ""

    if due_date:
        try:
            parsed = datetime.strptime(due_date, "%Y-%m-%d")
        except Exception:
            parsed = None

        if parsed:
            applescript = f'''
            tell application "Reminders"
              try
                set theList to (first list whose name is "{list_esc}")
              on error
                set theList to make new list with properties {{name:"{list_esc}"}}
              end try
              set dueDt to (current date)
              set year of dueDt to {parsed.year}
              set month of dueDt to {parsed.month}
              set day of dueDt to {parsed.day}
              set hours of dueDt to 9
              set minutes of dueDt to 0
              set seconds of dueDt to 0
              tell theList
                make new reminder with properties {{name:"{title_esc}", due date:dueDt{body_prop}}}
              end tell
            end tell
            '''
        else:
            applescript = f'''
            tell application "Reminders"
              try
                set theList to (first list whose name is "{list_esc}")
              on error
                set theList to make new list with properties {{name:"{list_esc}"}}
              end try
              tell theList
                make new reminder with properties {{name:"{title_esc}"{body_prop}}}
              end tell
            end tell
            '''
    else:
        applescript = f'''
        tell application "Reminders"
          try
            set theList to (first list whose name is "{list_esc}")
          on error
            set theList to make new list with properties {{name:"{list_esc}"}}
          end try
          tell theList
            make new reminder with properties {{name:"{title_esc}"{body_prop}}}
          end tell
        end tell
        '''

    cp = run(["osascript", "-e", applescript])
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip()
    return True, ""


# -------------------------
# Things 3
# -------------------------
def create_things_todo(title: str, list_name: str = "", due_date: Optional[str] = None, note_title: str = "") -> tuple[bool, str]:
    """Create a to-do in Things 3 via URL scheme."""
    params: Dict[str, str] = {"title": title or ""}
    if due_date:
        params["when"] = due_date
    list_name = list_name or DEFAULT_THINGS_LIST
    if list_name:
        params["list"] = list_name
    if note_title:
        params["notes"] = f"From: {note_title}"

    url = "things:///add?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    cp = run(["open", url])
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip()
    return True, ""


# -------------------------
# Main
# -------------------------
def main() -> None:
    cloud_path = None
    tmp_dir = None
    pages_tmp_dir = None
    direct_images: List[Path] = []

    if len(sys.argv) > 1 and sys.argv[1].strip():
        pdf = os.path.expanduser(sys.argv[1])
        p = Path(pdf)
        if not p.exists():
            raise RuntimeError(f"File not found: {pdf}")
        if p.suffix.lower() != ".pdf":
            raise RuntimeError("Please select a PDF file.")
    else:
        pdf, direct_images, cloud_path, tmp_dir = get_notebook_pdf_from_cloud()
        if pdf:
            pdf = os.path.expanduser(pdf)

    success = False
    try:
        if direct_images:
            pages = direct_images
            if len(pages) > MAX_PAGES:
                pages = pages[:MAX_PAGES]
            print(f"🖼️ Using {len(pages)} thumbnail(s) from notebook")
        else:
            print("📄 PDF:", pdf)
            pages, pages_tmp_dir = pdf_to_images(pdf)
            print(f"🖼️ Converted to {len(pages)} image(s) @ {IMAGE_DPI} DPI")
        for i, img in enumerate(pages, start=1):
            print(f"  - page {i}: {img.name} ({img.stat().st_size/1024:.1f} KB)")

        result = openai_vision(pages)

        if PRINT_MODEL_RAW_OUTPUT:
            print("\n===== MODEL RAW OUTPUT =====\n")
            print(result if result else "(empty)")
            print("\n============================\n")

        cleaned = strip_code_fences(result)
        if not cleaned.strip():
            raise RuntimeError("Model returned empty content. Try lowering IMAGE_DPI or MAX_PAGES in your .env.")

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            raise RuntimeError(f"Model did not return valid JSON. First 800 chars:\n{cleaned[:800]}")

        notes = data.get("notes", [])
        if not isinstance(notes, list) or len(notes) == 0:
            raise RuntimeError("Model returned empty notes array. Ensure handwriting is visible.")

        today = time.strftime("%Y-%m-%d")
        created_titles: List[str] = []
        reminders_created = 0
        reminders_failed = 0
        reminders_failures: List[str] = []

        for idx, n in enumerate(notes, start=1):
            if not isinstance(n, dict):
                raise RuntimeError("Model returned a non-object note entry.")
            if not isinstance(n.get("action_items", []), list):
                raise RuntimeError("Model returned invalid action_items format (expected list).")

            title = sanitize_title(n.get("title") or f"reMarkable Notes - ({today}) #{idx}")

            tags = normalize_tags(n.get("tags"))
            body = (n.get("body") or "").strip()
            if not body:
                body = "## Notes - Key Points\n- (empty)\n"

            body = remove_action_items_section(body).strip()

            # Strip trailing hashtag-only lines
            body_lines = body.splitlines()
            while body_lines and re.match(r'^(#\S+\s*)+$', body_lines[-1].strip()):
                body_lines.pop()
            body = "\n".join(body_lines).strip()

            if not body.lstrip().startswith("#"):
                body = tags_line(tags) + "\n\n" + body

            # Distribute pages to notes
            note_idx = idx - 1
            if note_idx < len(pages):
                if note_idx == len(notes) - 1:
                    note_pages = pages[note_idx:]
                else:
                    note_pages = [pages[note_idx]]
            else:
                note_pages = []

            bear_create(title, body, open_note=(idx == len(notes)), images=note_pages or None)
            created_titles.append(title)

            if CREATE_REMINDERS:
                action_items = n.get("action_items", []) or []
                for ai in action_items:
                    if not isinstance(ai, dict):
                        raise RuntimeError("Model returned a non-object action item.")
                    ai_title = (ai.get("title") or "").strip()
                    if not ai_title:
                        continue
                    ai_due = ai.get("due_date")
                    if isinstance(ai_due, str):
                        ai_due = ai_due.strip() or None
                    else:
                        ai_due = None
                    ai_list = ai.get("list")
                    if not isinstance(ai_list, str) or not ai_list.strip():
                        ai_list = DEFAULT_REMINDERS_LIST

                    if TODO_APP in ("reminders", "both"):
                        ok, err = create_reminder(ai_title, list_name=ai_list.strip(), due_date=ai_due, note_title=title)
                        if ok:
                            reminders_created += 1
                        else:
                            reminders_failed += 1
                            reminders_failures.append(f"{ai_title}: {err}")
                    if TODO_APP in ("things", "both"):
                        ok, err = create_things_todo(ai_title, list_name=ai_list.strip(), due_date=ai_due, note_title=title)
                        if ok:
                            reminders_created += 1
                        else:
                            reminders_failed += 1
                            reminders_failures.append(f"{ai_title}: {err}")

        todo_app_label = {"reminders": "Reminders.app", "things": "Things 3", "both": "Reminders.app + Things 3"}.get(TODO_APP, TODO_APP)

        print(f"\n✅ Created {len(created_titles)} Bear note(s):")
        for t in created_titles:
            print(" -", t)

        if CREATE_REMINDERS:
            print(f"⏰ Created {reminders_created} action item(s) in {todo_app_label}")
            if reminders_failed:
                print(f"⚠️ Failed to create {reminders_failed} action item(s):")
                for msg in reminders_failures:
                    print(" -", msg)

        notify("reMarkable → Bear", f"✅ {len(created_titles)} note(s), {reminders_created} reminder(s)")
        success = True
    finally:
        if success and DELETE_AFTER_PROCESSING and cloud_path:
            try:
                rmapi_run(["rm", cloud_path])
                print(f"🗑️ Deleted from reMarkable cloud: {cloud_path}")
            except Exception as e:
                print(f"⚠️ Failed to delete from cloud: {cloud_path}")
                print("   ", str(e).strip())

        if pages_tmp_dir:
            shutil.rmtree(pages_tmp_dir, ignore_errors=True)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify("reMarkable → Bear", f"❌ Failed: {str(e)[:80]}")
        print("\n❌ ERROR\n")
        print(traceback.format_exc())
        raise
