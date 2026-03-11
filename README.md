# remarkable-to-bear

Convert handwritten [reMarkable](https://remarkable.com/) notes into structured [Bear](https://bear.app/) notes and action items in [Things 3](https://culturedcode.com/things/) or Apple Reminders using GPT-4o Vision.

## What it does

1. **Pulls** your "Quick Sheets" notebook from the reMarkable cloud (or accepts a local PDF)
2. **Converts** each page to an image
3. **Sends** the images to OpenAI's GPT-4o vision model
4. **Creates** Bear notes with structured markdown (agenda, decisions, key points, open questions)
5. **Creates** action items in Things 3 and/or Apple Reminders (configurable), with context and a deep link back to the Bear note
6. **Deletes** the processed notebook from the reMarkable cloud (optional)

## Requirements

- **macOS** (uses Bear.app, Things 3 / Reminders.app, and AppleScript)
- **Python 3.10+**
- **[rmapi](https://github.com/ddvk/rmapi)** — reMarkable cloud CLI (see setup below)
- **[poppler](https://poppler.freedesktop.org/)** — PDF to image conversion (`brew install poppler`)
- **[Bear](https://bear.app/)** — note-taking app (Mac App Store)
- **[Things 3](https://culturedcode.com/things/)** and/or **Apple Reminders** — for action items
- **OpenAI API key** with access to a vision model
- **Remarkable cloud access (optional)**

## Setup

```bash
# Clone the repo
git clone https://github.com/mtnnn/remarkable-to-bear.git
cd remarkable-to-bear

# Install system dependencies
brew install go poppler

# Install rmapi (reMarkable cloud CLI)
go install github.com/ddvk/rmapi@latest
# Add Go binaries to PATH (add this to your ~/.zshrc or ~/.bash_profile)
export PATH=$HOME/go/bin:$PATH

# Configure rmapi (first time only — authenticates with reMarkable cloud)
rmapi

# Create a Python virtual environment (requires Python 3.10+)
# macOS system Python is often 3.9, so use Homebrew Python if needed:
#   brew install python@3.13
python3 -m venv venv          # or: python3.13 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Copy the example config and add your API key
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=sk-...
```

## Usage

### Direct CLI

```bash
# Pull "Quick Sheets" from reMarkable cloud and process
python remarkable_to_bear.py

# Process a local PDF
python remarkable_to_bear.py /path/to/notes.pdf
```

### Raycast

This script includes [Raycast](https://www.raycast.com/) metadata and works as a Raycast Script Command:

1. Open Raycast → Extensions → Script Commands → Add Script Directory
2. Point it to the cloned repo folder
3. Run "reMarkable → Bear" from Raycast (optionally select a PDF)

### Other workflow triggers

**Apple Shortcuts**
Create a Shortcut with a "Run Shell Script" action:
```bash
cd /path/to/remarkable-to-bear && python remarkable_to_bear.py "$1"
```
You can trigger it from the menu bar, keyboard shortcut, or Siri.

**Automator / Folder Action**
Set up a Folder Action on a directory where you save reMarkable PDF exports. When a new PDF appears, Automator runs the script automatically.

**Alfred**
Create an Alfred Workflow with a "Run Script" action pointing to the script. Trigger it with a keyword like `rm2bear`.

**Keyboard Maestro**
Create a macro with an "Execute Shell Script" action. Bind it to a hotkey.

**Hazel**
Watch a folder for new PDF files and run the script when one appears.

**Cron / launchd**
Schedule periodic processing (e.g., every morning):
```bash
# crontab -e
0 9 * * * cd /path/to/remarkable-to-bear && python remarkable_to_bear.py
```

**fswatch (file system watcher)**
```bash
fswatch -o ~/Downloads/*.pdf | xargs -n1 -I{} python /path/to/remarkable-to-bear/remarkable_to_bear.py
```

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(required)* | Your OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model (must support vision) |
| `RMAPI_SEARCH_TERM` | `Quick sheets` | Notebook name to find in reMarkable cloud |
| `DELETE_AFTER_PROCESSING` | `true` | Delete notebook from cloud after processing |
| `CREATE_REMINDERS` | `true` | Create action items from handwritten notes |
| `TODO_APP` | `reminders` | Todo app: `reminders`, `things`, or `both` |
| `DEFAULT_REMINDERS_LIST` | `Work` | Default Reminders.app list |
| `DEFAULT_THINGS_LIST` | *(empty = Inbox)* | Default Things 3 project |
| `IMAGE_DPI` | `170` | DPI for PDF → image conversion (lower = smaller payload) |
| `MAX_PAGES` | `12` | Maximum pages to process |
| `DEBUG_RAW_OUTPUT` | `false` | Print raw model JSON for debugging |

## Customizing the prompt

Edit `prompt.txt` to change how the AI interprets your handwritten notes. You can:

- Change the output structure (different markdown sections)
- Add or remove fields from the JSON schema
- Change the language or handwriting interpretation rules
- Adjust tag generation rules
- Modify how action items are extracted

The script will use `prompt.txt` from the same directory. If the file is missing, a built-in default is used.

## How it works

```
reMarkable Cloud                    Local PDF
       │                                │
       ▼                                │
   rmapi pull                           │
       │                                │
       ▼                                ▼
    PDF file ──────────────────► pdftoppm (poppler)
                                        │
                                        ▼
                                   PNG images
                                        │
                                        ▼
                              OpenAI GPT-4o Vision
                                        │
                                        ▼
                                  Structured JSON
                                   ╱          ╲
                                  ▼            ▼
                            Bear notes    Things 3 / Reminders
```

## License

MIT
