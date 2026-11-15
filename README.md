# Gemini Textual Chat

A cross-platform terminal chat client for Google Gemini, built with [Textual](https://textual.textualize.io/). The application provides a ChatGPT-style terminal interface with streaming responses, conversation history, dynamic model selection, URL and Wikipedia analysis, clipboard tools, automatic follow-up questions, multiline prompts, configurable guardrails, and a built-in Tetris game.

## Features

- **Google Gemini integration** using the `google-genai` Python SDK.
- **Dynamic model selector** that asks the Gemini API which models are available for the supplied API key and displays usable text-generation models in the sidebar.
- **Streaming responses** so generated text appears progressively in the terminal.
- **Conversation management** with multiple chats, chat titles, persistent local history, new-chat creation, chat deletion, and chat cycling.
- **URL-aware prompts** that fetch readable context from links included in a prompt. Wikipedia links are handled through the Wikipedia API when possible.
- **Message tools** available from the context menu on prompts and responses:
  - Copy text to the clipboard
  - Ask a question about the selected message
  - Summarize the message
  - Ask for reliable sources
- **Copy response button** below each assistant response.
- **Automatic follow-up questions** generated after each response. Follow-up questions appear as separate clickable boxes and can be inserted into the prompt editor.
- **Multiline prompts** using the terminal text editor:
  - Press `Enter` to send a prompt.
  - Press `Shift+Enter` to insert a line break.
- **Guardrail pipeline** with prompt-injection detection, blocked-topic filtering, profanity replacement, PII redaction, and length limits.
- **Persistent local sessions** stored outside the repository in `.gemini_sessions.json`.
- **Built-in Tetris game** launched with `Ctrl+G`.
- **Terminal themes** with a theme toggle that preserves the original Textual theme behavior.
- **Cross-platform clipboard support** through `pyperclip` on Windows and macOS.

## Requirements

- Python 3.10 or newer is recommended.
- A Google Gemini API key.
- Windows, macOS, or another operating system capable of running Python and Textual.
- A terminal that supports interactive keyboard input. Windows Terminal and the VS Code integrated terminal are recommended on Windows.

## Installation on Windows

Open PowerShell in the project folder and run:

```powershell
py -m pip install -r requirements-textual.txt
```

Create a file named `api_key.txt` in the same folder as `chat_textual.py`. Paste only your Gemini API key into that file:

```text
AIzaSy-your-key-goes-here
```

Start the application:

```powershell
py chat_textual.py
```

If the `py` launcher is not available, use `python` instead:

```powershell
python -m pip install -r requirements-textual.txt
python chat_textual.py
```

## Installation on macOS

Open Terminal in the project folder and run:

```bash
python3 -m pip install -r requirements-textual.txt
```

Create `api_key.txt` beside `chat_textual.py`, then start the application:

```bash
python3 chat_textual.py
```

## API-key security

Never commit your Gemini API key to GitHub. The local key file should be named exactly:

```text
api_key.txt
```

The repository's `.gitignore` should include:

```gitignore
api_key.txt
.env
.gemini_sessions.json
.tetris_high_score
```

The application first looks for `api_key.txt` beside the Python script. It also supports these optional environment variables as fallbacks:

```text
GEMINI_API_KEY
GOOGLE_API_KEY
```

If a key has accidentally been committed to a public repository, revoke it in Google AI Studio or Google Cloud immediately, create a replacement key, and remove the exposed key from the repository history.

## Using the application

When the application starts, the sidebar displays a model selector. The application attempts to retrieve models available to the configured API key. Only Gemini models that appear to support text generation are shown.

The active model is shown in the terminal header. Selecting a different model changes the model used for subsequent requests.

Type a prompt in the text area and press `Enter` or click **Send**. To write a multiline prompt, press `Shift+Enter` between lines. Pasted text containing line breaks is retained as a multiline prompt.

After an assistant response is complete, the application generates up to three possible follow-up questions. Click one of the follow-up boxes to place that question in the prompt editor.

Right-click a prompt or response bubble to open the message actions menu. The response also has a dedicated **Copy response** button below it.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+N` | Create a new chat |
| `Ctrl+D` | Delete the current chat |
| `Ctrl+Q` | Quit the application |
| `Ctrl+T` | Toggle the terminal theme |
| `Ctrl+B` | Show or hide the sidebar |
| `Ctrl+E` | Cycle through chats |
| `Ctrl+K` | Show the keyboard-help panel, if available in the current version |
| `Ctrl+G` | Launch Tetris |
| `Enter` | Send the current prompt |
| `Shift+Enter` | Insert a line break in the prompt |
| Right-click | Open message actions for a prompt or response |

## URL and Wikipedia analysis

If a prompt contains one or more HTTP or HTTPS links, the application attempts to retrieve readable text from up to three links and supplies that context to Gemini. For Wikipedia links, the application attempts to use the relevant Wikipedia API endpoint to obtain plain-text article content.

For example:

```text
Summarize this article and identify its key claims:
https://en.wikipedia.org/wiki/Artificial_intelligence
```

The model is instructed to distinguish fetched source content from its own reasoning and to cite the source URL when appropriate. Link fetching can fail because of network restrictions, unavailable pages, rate limits, robots policies, or invalid URLs. A failed fetch should not be treated as proof that the source does not exist.

## Guardrails

The application applies local input and output checks before displaying or storing messages. The default rules include:

- Prompt-injection phrase detection
- Blocked-topic detection
- Email, phone, SSN, and credit-card redaction
- Profanity replacement
- Maximum input and output lengths

These are convenience safeguards, not a complete security or moderation system. They should not be treated as a substitute for Google safety settings, application security review, or human judgment.

## Project files

```text
gemini-textual-chat/
├── chat_textual.py
├── requirements-textual.txt
├── README.md
└── .gitignore
```

Local-only files that should not be committed:

```text
api_key.txt
.gemini_sessions.json
.tetris_high_score
```

## Dependencies

The project uses:

- `google-genai` for Gemini API requests
- `textual` for the terminal user interface
- `pyperclip` for cross-platform clipboard operations
- `pygame` for the optional Tetris game

Install all dependencies with:

```powershell
py -m pip install -r requirements-textual.txt
```

## Troubleshooting

### No models appear or the model list is incorrect

Confirm that `api_key.txt` is in the same folder as `chat_textual.py` and contains only a valid Gemini API key. Restart the application after changing the key. The available model list depends on the API key, project, region, account, and current Google API availability.

### The assistant reports the wrong model

The model ID shown in the terminal header is the authoritative value for the request. The assistant can sometimes hallucinate technical details. The application includes the active model ID in the system instruction to reduce this behavior.

### Clipboard copying does not work

Confirm that `pyperclip` installed successfully:

```powershell
py -m pip install --upgrade pyperclip
```

On Windows, use Windows Terminal or the VS Code terminal if possible. Clipboard behavior can vary between terminal hosts.

### Tetris does not launch

Confirm that `pygame` is installed:

```powershell
py -m pip install --upgrade pygame
```

Tetris is optional; the chat client can still be used without launching it.

### A URL cannot be analyzed

Check that the URL begins with `http://` or `https://`, is publicly reachable, and does not require login. Some sites block automated requests or render their content entirely with JavaScript.

## License

Choose a license before publishing the repository. The MIT License is a common choice for a small, reusable personal project, but the appropriate license depends on how you want others to use and redistribute the code.
