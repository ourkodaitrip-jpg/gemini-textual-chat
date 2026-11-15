"""
chat_textual.py — Gemini AI Chat, rebuilt with Textual for a ChatGPT-style feel.

A sidebar lists your conversations (click one, or hit "+ New chat"), the
main pane shows messages as chat bubbles, and replies stream in token by
token. Same guardrail pipeline as the original script (prompt-injection,
blocked topics, profanity filter, PII redaction, max length).

Setup:
    pip install -r requirements-textual.txt
    Create api_key.txt beside this script and paste only your Google Gemini API key into it.
    Windows PowerShell:
        py -m pip install -r requirements-textual.txt
        py chat_textual.py
    macOS:
        python3 -m pip install -r requirements-textual.txt
        python3 chat_textual.py
    The app uses pyperclip for Windows/macOS clipboard support.

Run:
    python chat_textual.py

Keys: ctrl+n new chat · ctrl+d delete chat · ctrl+b toggle sidebar ·
      ctrl+e cycle chats · ctrl+q quit · ctrl+g play Tetris
      Right-click any message bubble for Copy / Ask about this / Summarize /
      Ask for sources. Enter sends; Shift+Enter inserts a line break.
"""

from __future__ import annotations

import json
import os
import re
import threading
import asyncio
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

from google import genai
from google.genai import types

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Select, Static, TextArea

FALLBACK_MODELS = [
    ("Gemini 2.0 Flash", "gemini-2.0-flash"),
    ("Gemini 2.0 Flash Lite", "gemini-2.0-flash-lite"),
    ("Gemini 1.5 Flash", "gemini-1.5-flash"),
]
DEFAULT_MODEL = FALLBACK_MODELS[0][1]
MAX_COMPLETION_TOKENS = 1024
SYSTEM_PROMPT = """
You are a no-nonsense terminal-based assistant. 
Do not hallucinate and give fake data. 
If data is not found, please tell that data is not found.
"""

HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".gemini_sessions.json")
TETRIS_HIGH_SCORE_FILE = os.path.join(os.path.expanduser("~"), ".tetris_high_score")


def load_api_key() -> str | None:
    """Read api_key.txt beside this script, falling back to GEMINI_API_KEY."""
    key_file = Path(__file__).with_name("api_key.txt")
    try:
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_link_context(text: str, limit: int = 12000) -> str:
    """Fetch readable context for URLs in a prompt, including Wikipedia pages."""
    urls = re.findall(r"https?://[^\s<>\"']+", text)
    contexts = []
    for raw_url in urls[:3]:
        url = raw_url.rstrip(".,);]}")
        try:
            parsed = urlparse(url)
            if "wikipedia.org" in parsed.netloc and "/wiki/" in parsed.path:
                title = unquote(parsed.path.split("/wiki/", 1)[1])
                api_url = "https://" + parsed.netloc.split(".")[0] + ".wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&redirects=1&format=json&titles=" + title
                request_url = api_url
            else:
                request_url = url
            req = Request(request_url, headers={"User-Agent": "GeminiTextualChat/1.0"})
            with urlopen(req, timeout=12) as response:
                raw = response.read(500000).decode("utf-8", errors="replace")
            if request_url.endswith("format=json") or "format=json&" in request_url:
                data = json.loads(raw)
                pages = data.get("query", {}).get("pages", {})
                content = next(iter(pages.values()), {}).get("extract", "")
            else:
                parser = _VisibleTextParser()
                parser.feed(raw)
                content = " ".join(" ".join(parser.parts).split())
            if content:
                contexts.append(f"SOURCE URL: {url}\nSOURCE CONTENT:\n{content[:limit]}")
        except Exception as exc:
            contexts.append(f"SOURCE URL: {url}\nSOURCE FETCH ERROR: {exc}")
    return "\n\n".join(contexts)


# ══════════════════════════════════════════
#  Guardrail rules — unchanged from the original script
# ══════════════════════════════════════════

@dataclass
class GuardrailViolation:
    rule: str
    message: str
    blocked: bool = True


@dataclass
class GuardrailResult:
    text: str
    violations: list = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return any(v.blocked for v in self.violations)


class GuardrailRule:
    def check(self, text: str) -> GuardrailResult:
        return GuardrailResult(text=text)


class BlockedTopicsRule(GuardrailRule):
    TOPICS = ["how to make a bomb", "synthesize drugs", "hack into", "create malware", "child exploitation"]

    def __init__(self, topics=None):
        self.topics = [t.lower() for t in (topics or self.TOPICS)]

    def check(self, text):
        lowered = text.lower()
        for t in self.topics:
            if t in lowered:
                return GuardrailResult(text=text, violations=[GuardrailViolation("BlockedTopics", f"Blocked topic: '{t}'")])
        return GuardrailResult(text=text)


class ProfanityFilterRule(GuardrailRule):
    WORDS = ["badword1", "badword2", "damn", "crap"]

    def __init__(self, mode="replace", words=None):
        self.mode = mode
        self.words = [w.lower() for w in (words or self.WORDS)]

    def check(self, text):
        found = [w for w in self.words if w in text.lower()]
        if not found:
            return GuardrailResult(text=text)
        if self.mode == "block":
            return GuardrailResult(text=text, violations=[GuardrailViolation("ProfanityFilter", f"Profanity: {found}")])
        cleaned = text
        for w in found:
            cleaned = re.sub(re.escape(w), "***", cleaned, flags=re.IGNORECASE)
        return GuardrailResult(text=cleaned, violations=[GuardrailViolation("ProfanityFilter", f"Profanity replaced: {found}", blocked=False)])


class PIIRedactionRule(GuardrailRule):
    PATTERNS = {
        "email": (r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[EMAIL]"),
        "phone": (r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[PHONE]"),
        "ssn": (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
        "credit_card": (r"\b(?:\d[ -]?){13,16}\b", "[CARD]"),
    }

    def __init__(self, patterns=None, blocked=False):
        self.patterns = patterns or self.PATTERNS
        self.blocked = blocked

    def check(self, text):
        violations, cleaned = [], text
        for name, (pat, rep) in self.patterns.items():
            if re.search(pat, cleaned):
                if self.blocked:
                    return GuardrailResult(text=text, violations=[GuardrailViolation("PIIRedaction", f"PII ({name}) blocked")])
                cleaned = re.sub(pat, rep, cleaned)
                violations.append(GuardrailViolation("PIIRedaction", f"PII redacted: {name}", blocked=False))
        return GuardrailResult(text=cleaned, violations=violations)


class MaxLengthRule(GuardrailRule):
    def __init__(self, max_chars=4000, action="block"):
        self.max_chars = max_chars
        self.action = action

    def check(self, text):
        if len(text) <= self.max_chars:
            return GuardrailResult(text=text)
        if self.action == "truncate":
            return GuardrailResult(text=text[: self.max_chars] + " [truncated]", violations=[GuardrailViolation("MaxLength", f"Truncated to {self.max_chars}", blocked=False)])
        return GuardrailResult(text=text, violations=[GuardrailViolation("MaxLength", f"Too long ({len(text)} chars)")])


class PromptInjectionRule(GuardrailRule):
    PATS = [
        r"ignore (all |previous |prior |above )?(instructions?|rules?)",
        r"you are now",
        r"pretend (you are|to be|that)",
        r"disregard (your|all|previous)",
        r"jailbreak",
        r"do anything now",
        r"dan mode",
    ]

    def check(self, text):
        lowered = text.lower()
        for p in self.PATS:
            if re.search(p, lowered):
                return GuardrailResult(text=text, violations=[GuardrailViolation("PromptInjection", "Prompt injection detected")])
        return GuardrailResult(text=text)


def run_pipeline(text: str, rules: list) -> GuardrailResult:
    cur, violations = text, []
    for rule in rules:
        result = rule.check(cur)
        violations.extend(result.violations)
        if result.is_blocked:
            return GuardrailResult(text=cur, violations=violations)
        cur = result.text
    return GuardrailResult(text=cur, violations=violations)


INPUT_RULES = [PromptInjectionRule(), BlockedTopicsRule(), PIIRedactionRule(), ProfanityFilterRule(mode="replace"), MaxLengthRule(4000, "block")]
OUTPUT_RULES = [PIIRedactionRule(), MaxLengthRule(8000, "truncate")]


# ══════════════════════════════════════════
#  Session storage — same JSON file/format as the original script
# ══════════════════════════════════════════

def load_sessions() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_sessions(sessions: list) -> None:
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception:
        pass


def new_session() -> dict:
    return {
        "id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "title": "New chat",
        "created": datetime.now().strftime("%b %d %H:%M"),
        "history": [],
        "log": [],
    }


# ══════════════════════════════════════════
#  Tetris — launched in a separate thread via Ctrl+G
# ══════════════════════════════════════════

def tetris():
    import pygame
    import random
    import time
    import sys

    # --- Settings & Colors ---
    SCREEN_WIDTH, SCREEN_HEIGHT = 700, 650
    BLOCK_SIZE = 25
    GRID_WIDTH, GRID_HEIGHT = 10, 20
    FLASH_COLOR = (255, 255, 255)
    FLASH_DURATION = 6

    BOARD_X, BOARD_Y = 220, 90
    BOARD_W, BOARD_H = GRID_WIDTH * BLOCK_SIZE, GRID_HEIGHT * BLOCK_SIZE

    BG_COLOR = (13, 15, 20)
    BOARD_BG = (21, 23, 30)
    PANEL_BG = (24, 27, 35)
    PANEL_BORDER = (42, 46, 58)
    ACCENT_COLOR = (90, 210, 200)
    TEXT_COLOR = (222, 228, 242)
    LABEL_COLOR = (130, 138, 158)
    LINE_COLOR = (33, 36, 46)
    GHOST_COLOR = (80, 84, 96)

    colors = [
        (0, 0, 0),
        (86, 214, 214),   # I - cyan
        (232, 197, 71),   # O - gold
        (176, 104, 214),  # T - purple
        (99, 209, 129),   # S - green
        (222, 96, 96),    # Z - red
        (92, 138, 222),   # J - blue
        (232, 149, 74),   # L - orange
    ]

    def load_high_score():
        try:
            with open(TETRIS_HIGH_SCORE_FILE) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def save_high_score(value):
        try:
            with open(TETRIS_HIGH_SCORE_FILE, "w") as f:
                f.write(str(value))
        except OSError:
            pass

    class Figure:
        figures = [
            [[1, 5, 9, 13], [4, 5, 6, 7]],                   # I
            [[1, 2, 5, 6]],                                   # O
            [[1, 4, 5, 6], [1, 4, 5, 9], [4, 5, 6, 9], [1, 5, 6, 9]], # T
            [[4, 5, 9, 10], [2, 6, 5, 9]],                   # S
            [[6, 7, 9, 10], [1, 5, 6, 10]],                  # Z
            [[1, 2, 5, 9], [0, 4, 5, 6], [1, 5, 9, 8], [4, 5, 6, 10]], # J
            [[1, 2, 6, 10], [5, 6, 7, 9], [2, 6, 10, 11], [3, 5, 6, 7]]  # L
        ]

        def __init__(self, x, y):
            self.x, self.y = x, y
            self.type = random.randint(0, len(self.figures) - 1)
            self.color = random.randint(1, len(colors) - 1)
            self.rotation = 0

        def image(self): return self.figures[self.type][self.rotation]
        def rotate(self): self.rotation = (self.rotation + 1) % len(self.figures[self.type])

    class Tetris:
        def __init__(self):
            self.field = [[0 for _ in range(GRID_WIDTH)] for _ in range(GRID_HEIGHT)]
            self.score, self.level = 0, 1
            self.state = "splash"
            self.x, self.y = BOARD_X, BOARD_Y
            self.figure = None
            self.next_figure = Figure(3, 0)
            self.held_figure = None
            self.can_hold = True
            self.start_time = time.time()
            self.gravity_counter, self.flash_counter = 0, 0
            self.cleared_rows = []
            self.final_score = 0
            self.high_score = load_high_score()
            self.new_high_score = False

        def update_high_score(self):
            if self.score > self.high_score:
                self.high_score = self.score
                save_high_score(self.high_score)
                self.new_high_score = True

        def lose(self):
            self.final_score = self.score
            self.update_high_score()
            self.state = "gameover"

        def reset(self):
            high = self.high_score
            self.__init__()
            self.high_score = high
            self.state = "start"
            self.new_figure()

        def new_figure(self):
            self.figure = self.next_figure
            self.next_figure = Figure(3, 0)
            self.can_hold = True
            if self.intersects(): self.lose()

        def rotate(self):
            if not self.figure: return
            old_rot, old_x, old_y = self.figure.rotation, self.figure.x, self.figure.y
            self.figure.rotate()
            kicks = [(0,0), (-1,0), (1,0), (0,-1), (-2,0), (2,0)]
            success = False
            for dx, dy in kicks:
                self.figure.x += dx
                self.figure.y += dy
                if not self.intersects():
                    success = True
                    break
                self.figure.x, self.figure.y = old_x, old_y
            if not success: self.figure.rotation = old_rot

        def hold_piece(self):
            if not self.can_hold or not self.figure: return
            if self.held_figure is None:
                self.held_figure = Figure(3, 0)
                self.held_figure.type, self.held_figure.color = self.figure.type, self.figure.color
                self.new_figure()
            else:
                self.figure.type, self.held_figure.type = self.held_figure.type, self.figure.type
                self.figure.color, self.held_figure.color = self.held_figure.color, self.figure.color
                self.figure.rotation, self.held_figure.rotation = 0, 0
                self.figure.x, self.figure.y = 3, 0
            self.can_hold = False

        def intersects(self):
            for i in range(4):
                for j in range(4):
                    if i * 4 + j in self.figure.image():
                        if i + self.figure.y > GRID_HEIGHT - 1 or j + self.figure.x > GRID_WIDTH - 1 or \
                           j + self.figure.x < 0 or self.field[i + self.figure.y][j + self.figure.x] > 0:
                            return True
            return False

        def apply_gravity(self):
            for j in range(GRID_WIDTH):
                column = [self.field[i][j] for i in range(GRID_HEIGHT)]
                settled = [v for v in column if v != 0]
                settled = [0] * (GRID_HEIGHT - len(settled)) + settled
                for i in range(GRID_HEIGHT):
                    self.field[i][j] = settled[i]

        def freeze(self):
            for i in range(4):
                for j in range(4):
                    if i * 4 + j in self.figure.image():
                        self.field[i + self.figure.y][j + self.figure.x] = self.figure.color
            self.apply_gravity()
            self.cleared_rows = [i for i, row in enumerate(self.field) if 0 not in row]
            if self.cleared_rows:
                self.state = "line_clear"
                self.flash_counter = FLASH_DURATION
            else:
                self.new_figure()

        def process_line_clear(self):
            self.score += (len(self.cleared_rows) ** 2) * 100
            self.update_high_score()
            kept = [row for i, row in enumerate(self.field) if i not in self.cleared_rows]
            self.field = [[0] * GRID_WIDTH for _ in range(len(self.cleared_rows))] + kept
            self.apply_gravity()
            self.cleared_rows = [i for i, row in enumerate(self.field) if 0 not in row]
            if self.cleared_rows:
                self.state = "line_clear"
                self.flash_counter = FLASH_DURATION
            else:
                self.level = 1 + (self.score // 1000)
                self.cleared_rows = []
                self.state = "start"
                self.new_figure()

    # --- Execution Loop ---
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption("Tetris 2026")
    clock = pygame.time.Clock()
    game = Tetris()
    f_path = pygame.font.match_font('consolas')
    f_small = pygame.font.Font(f_path, 15)
    f_med = pygame.font.Font(f_path, 22)
    f_large = pygame.font.Font(f_path, 46)
    f_label = pygame.font.Font(f_path, 13)

    def draw_panel(rect, label, label_font=f_label):
        pygame.draw.rect(screen, PANEL_BG, rect, border_radius=8)
        pygame.draw.rect(screen, PANEL_BORDER, rect, 1, border_radius=8)
        text = label_font.render(label, True, LABEL_COLOR)
        screen.blit(text, (rect[0] + (rect[2] - text.get_width()) // 2, rect[1] + 8))

    def draw_piece_preview(fig, rect):
        if not fig: return
        cells = [(i, j) for i in range(4) for j in range(4) if i * 4 + j in fig.figures[fig.type][0]]
        min_i = min(c[0] for c in cells)
        min_j = min(c[1] for c in cells)
        max_i = max(c[0] for c in cells)
        max_j = max(c[1] for c in cells)
        span_i, span_j = max_i - min_i + 1, max_j - min_j + 1
        cell = 16
        ox = rect[0] + (rect[2] - span_j * cell) // 2
        oy = rect[1] + 34 + (rect[3] - 34 - span_i * cell) // 2
        for i, j in cells:
            pygame.draw.rect(screen, colors[fig.color],
                             [ox + (j - min_j) * cell, oy + (i - min_i) * cell, cell - 2, cell - 2],
                             border_radius=3)

    while True:
        screen.fill(BG_COLOR)
        if game.state == "line_clear":
            game.flash_counter -= 1
            if game.flash_counter <= 0: game.process_line_clear()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return   # close window without killing the chat app
            if game.state == "splash" and (event.type in [pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN]):
                game.state = "start"; game.new_figure()
            elif game.state == "gameover" and (event.type in [pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN]):
                game.reset()
            elif game.state == "start":
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_r: game.reset()
                    if event.key == pygame.K_UP: game.rotate()
                    if event.key == pygame.K_LEFT:
                        game.figure.x -= 1
                        if game.intersects(): game.figure.x += 1
                    if event.key == pygame.K_RIGHT:
                        game.figure.x += 1
                        if game.intersects(): game.figure.x -= 1
                    if event.key == pygame.K_DOWN: game.gravity_counter = 30
                    if event.key == pygame.K_SPACE:
                        while not game.intersects(): game.figure.y += 1
                        game.figure.y -= 1; game.freeze()
                    if event.key == pygame.K_c: game.hold_piece()
                if event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1: game.rotate()
                    if event.button == 3: game.hold_piece()

        if game.state == "splash":
            title = f_large.render("TETRIS", True, TEXT_COLOR)
            subtitle = f_small.render("2026 EDITION", True, ACCENT_COLOR)
            screen.blit(title, ((SCREEN_WIDTH - title.get_width()) // 2, 180))
            screen.blit(subtitle, ((SCREEN_WIDTH - subtitle.get_width()) // 2, 235))
            lines = ["ARROWS  Move", "UP / L-CLICK  Rotate", "C / R-CLICK  Hold",
                     "SPACE  Hard Drop", "", "PRESS ANY KEY TO START"]
            for i, text in enumerate(lines):
                color = ACCENT_COLOR if "START" in text else LABEL_COLOR
                surf = f_small.render(text, True, color)
                screen.blit(surf, ((SCREEN_WIDTH - surf.get_width()) // 2, 300 + i * 28))

        elif game.state in ["start", "line_clear"]:
            if game.state == "start" and game.figure:
                delay = max(2, 22 - (game.level * 2))
                game.gravity_counter += 1
                if game.gravity_counter >= delay:
                    game.figure.y += 1
                    if game.intersects(): game.figure.y -= 1; game.freeze()
                    game.gravity_counter = 0

            pygame.draw.rect(screen, BOARD_BG, [game.x - 3, game.y - 3, BOARD_W + 6, BOARD_H + 6], border_radius=6)
            pygame.draw.rect(screen, PANEL_BORDER, [game.x - 3, game.y - 3, BOARD_W + 6, BOARD_H + 6], 2, border_radius=6)
            for i in range(GRID_HEIGHT):
                for j in range(GRID_WIDTH):
                    cell_x, cell_y = game.x + j * BLOCK_SIZE, game.y + i * BLOCK_SIZE
                    pygame.draw.rect(screen, LINE_COLOR, [cell_x, cell_y, BLOCK_SIZE, BLOCK_SIZE], 1)
                    val = game.field[i][j]
                    if val != 0:
                        col = FLASH_COLOR if (game.state == "line_clear" and i in game.cleared_rows) else colors[val]
                        pygame.draw.rect(screen, col, [cell_x + 1, cell_y + 1, BLOCK_SIZE - 2, BLOCK_SIZE - 2], border_radius=3)

            if game.state == "start" and game.figure:
                old_y = game.figure.y
                while not game.intersects(): game.figure.y += 1
                game.figure.y -= 1
                for i in range(4):
                    for j in range(4):
                        if i * 4 + j in game.figure.image():
                            pygame.draw.rect(screen, GHOST_COLOR,
                                             [game.x + (game.figure.x + j) * BLOCK_SIZE,
                                              game.y + (game.figure.y + i) * BLOCK_SIZE,
                                              BLOCK_SIZE, BLOCK_SIZE], 1, border_radius=3)
                game.figure.y = old_y
                for i in range(4):
                    for j in range(4):
                        if i * 4 + j in game.figure.image():
                            pygame.draw.rect(screen, colors[game.figure.color],
                                             [game.x + (game.figure.x + j) * BLOCK_SIZE + 1,
                                              game.y + (game.figure.y + i) * BLOCK_SIZE + 1,
                                              BLOCK_SIZE - 2, BLOCK_SIZE - 2], border_radius=3)

            right_x = game.x + BOARD_W + 30
            panel_w = SCREEN_WIDTH - right_x - 30

            score_rect = [right_x, game.y, panel_w, 64]
            draw_panel(score_rect, "SCORE")
            score_surf = f_med.render(str(game.score), True, TEXT_COLOR)
            screen.blit(score_surf, (right_x + (panel_w - score_surf.get_width()) // 2, game.y + 30))

            level_rect = [right_x, game.y + 76, panel_w, 64]
            draw_panel(level_rect, "LEVEL")
            level_surf = f_med.render(str(game.level), True, TEXT_COLOR)
            screen.blit(level_surf, (right_x + (panel_w - level_surf.get_width()) // 2, game.y + 76 + 30))

            high_rect = [right_x, game.y + 152, panel_w, 64]
            draw_panel(high_rect, "HIGH SCORE")
            high_surf = f_med.render(str(game.high_score), True, ACCENT_COLOR)
            screen.blit(high_surf, (right_x + (panel_w - high_surf.get_width()) // 2, game.y + 152 + 30))
            if game.new_high_score:
                new_high_surf = f_label.render("New High Score!", True, ACCENT_COLOR)
                screen.blit(new_high_surf, (right_x + (panel_w - new_high_surf.get_width()) // 2, game.y + 152 + 66))

            next_rect = [right_x, game.y + 244, panel_w, 100]
            draw_panel(next_rect, "NEXT")
            draw_piece_preview(game.next_figure, next_rect)

            hold_x = game.x - 30 - 110
            hold_rect = [hold_x, game.y, 110, 100]
            draw_panel(hold_rect, "HOLD")
            draw_piece_preview(game.held_figure, hold_rect)

        elif game.state == "gameover":
            over = f_large.render("GAME OVER", True, (232, 96, 96))
            score_line = f_med.render(f"Final Score  {game.final_score}", True, TEXT_COLOR)
            high_line = f_med.render(f"High Score  {game.high_score}", True, ACCENT_COLOR)
            hint = f_small.render("PRESS ANY KEY TO RESTART", True, ACCENT_COLOR)
            screen.blit(over, ((SCREEN_WIDTH - over.get_width()) // 2, 250))
            screen.blit(score_line, ((SCREEN_WIDTH - score_line.get_width()) // 2, 320))
            screen.blit(high_line, ((SCREEN_WIDTH - high_line.get_width()) // 2, 355))
            screen.blit(hint, ((SCREEN_WIDTH - hint.get_width()) // 2, 405))

        pygame.display.flip()
        clock.tick(30)


def run_tetris():
    """Launch Tetris in a daemon thread so it doesn't block the Textual event loop."""
    t = threading.Thread(target=tetris, daemon=True)
    t.start()


# ══════════════════════════════════════════
#  Widgets
# ══════════════════════════════════════════

class MessageBubble(Static):
    """A single chat message. markup=False because guardrail text like
    '[Blocked]' or '[EMAIL]' would otherwise be parsed as Rich markup."""

    def __init__(self, role: str, text: str = ""):
        super().__init__(text, classes=f"bubble {role}", markup=False)
        self.role = role
        self.full_text = text

    def append_text(self, chunk: str) -> None:
        self.full_text += chunk
        self.update(self.full_text)

    def set_text(self, text: str) -> None:
        self.full_text = text
        self.update(text)

    def on_click(self, event: events.Click) -> None:
        if event.button == 3:  # right-click
            event.stop()
            self.app.show_bubble_menu(self, event.screen_x, event.screen_y)


class MessageRow(Horizontal):
    """Aligns a bubble right for the user, left for assistant/system."""

    def __init__(self, bubble: MessageBubble):
        super().__init__(classes=f"row {bubble.role}")
        self.bubble = bubble

    def compose(self) -> ComposeResult:
        yield self.bubble
        if self.bubble.role == "assistant":
            yield Button("Copy response", classes="copy-response")


class FollowUpBox(Button):
    """A generated follow-up that can be clicked to put it in the prompt box."""

    def __init__(self, question: str):
        super().__init__(question, classes="follow-up", name=question)
        self.question = question


class PromptTextArea(TextArea):
    """Enter sends; Shift+Enter inserts a newline for pasted/multiline prompts."""

    def on_key(self, event: events.Key) -> None:
        if event.key in {"shift+enter", "ctrl+enter"}:
            self.insert("\n")
            event.stop()
        elif event.key == "enter":
            event.stop()
            self.app.send_message(self.text)


class ContextMenu(Vertical):
    """Small right-click menu for a message bubble. This is mounted directly
    onto the current screen (not pushed as a new modal screen), so it floats
    over the chat log and background instead of covering/replacing them."""

    DEFAULT_CSS = """
    ContextMenu {
        layer: overlay;
        width: 26;
        height: auto;
        padding: 1 0;
        border: round $primary-background;
        background: $surface;
    }
    ContextMenu Button {
        width: 100%;
        height: 3;
        border: none;
        background: transparent;
        content-align: left middle;
        padding: 0 2;
    }
    ContextMenu Button:hover {
        background: $boost;
    }
    """

    def compose(self) -> ComposeResult:
        yield Button("Copy", id="copy", classes="menu-btn")
        yield Button("Ask about this", id="ask", classes="menu-btn")
        yield Button("Summarize", id="summarize", classes="menu-btn")
        yield Button("Ask for sources", id="source", classes="menu-btn")



class SessionItem(ListItem):
    """One row in the sidebar session list."""

    def __init__(self, index: int, title: str):
        super().__init__(Label(title, markup=False))
        self.session_index = index


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation dialog, used for deleting a chat."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen #dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    ConfirmScreen #dialog Label {
        margin-bottom: 1;
    }
    ConfirmScreen #buttons {
        align-horizontal: right;
        height: auto;
    }
    ConfirmScreen #buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message, markup=False)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="confirm", variant="error")

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def confirm(self) -> None:
        self.dismiss(True)


# ══════════════════════════════════════════
#  Main app
# ══════════════════════════════════════════

class ChatApp(App):
    """ChatGPT-feeling terminal client for Gemini models, with guardrails."""

    TITLE = "Gemini Chat"

    CSS = """
    Screen {
        background: $background;
        layers: base overlay;
    }

    #sidebar {
        width: 30;
        background: $panel;
        border-right: solid $primary-background;
        padding: 1;
    }

    #new-chat-btn {
        width: 100%;
        margin-bottom: 1;
    }

    #session-list {
        background: transparent;
        height: 1fr;
    }

    #main {
        width: 1fr;
    }

    #chat-log {
        height: 1fr;
        padding: 1 3;
        background: $background;
    }

    .empty-state {
        width: 100%;
        height: 100%;
        content-align: center middle;
        color: $text-muted;
    }

    .row {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    .row.user {
        align-horizontal: right;
    }

    .row.assistant, .row.system {
        align-horizontal: left;
    }

    .bubble {
        width: auto;
        max-width: 80%;
        height: auto;
        padding: 1 2;
        border: round $primary-background;
    }

    .bubble.user {
        background: $primary;
        color: $text;
    }

    .bubble.assistant {
        background: $boost;
    }

    .bubble.system {
        background: $error 30%;
        border: round $error;
    }

    .copy-response {
        width: auto;
        min-width: 16;
        height: 3;
        margin: 1 0 0 1;
    }

    .follow-up {
        width: 100%;
        height: auto;
        min-height: 3;
        margin: 0 3 1 3;
        padding: 0 1;
        border: round $primary-background;
        background: $surface;
        content-align: left middle;
    }

    #model-selector {
        width: 100%;
        margin-bottom: 1;
    }

    #input-bar {
        height: auto;
        padding: 1;
        background: $boost;
    }

    #chat-input {
        width: 1fr;
        margin-right: 1;
        height: 5;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_chat", "New chat", key_display="ctrl+n"),
        Binding("ctrl+d", "delete_chat", "Delete chat", key_display="ctrl+d"),
        Binding("ctrl+q", "quit", "Quit", key_display="ctrl+q"),
        Binding("ctrl+t", "toggle_theme", "Toggle theme", key_display="ctrl+t"),
        Binding("ctrl+b", "toggle_sidebar", "Toggle sidebar", key_display="ctrl+b"),
        Binding("ctrl+e", "cycle_chat", "Cycle chats", key_display="ctrl+e"),
        Binding("ctrl+k", "show_help_panel", "Show keys", key_display="ctrl+k"),
        Binding("ctrl+g", "play_tetris", "Play Tetris", key_display="ctrl+g"),
    ]

    def __init__(self):
        super().__init__()
        api_key = load_api_key()
        self.client = genai.Client(api_key=api_key) if api_key else None
        self.selected_model = DEFAULT_MODEL
        self.sessions = load_sessions() or [new_session()]
        self.current = 0
        self._context_menu: ContextMenu | None = None
        self._context_menu_bubble: MessageBubble | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Button("+ New chat", id="new-chat-btn")
                yield Select([(label, value) for label, value in FALLBACK_MODELS], value=DEFAULT_MODEL, id="model-selector")
                yield ListView(id="session-list")
            with Vertical(id="main"):
                yield VerticalScroll(id="chat-log")
                with Horizontal(id="input-bar"):
                    yield PromptTextArea(placeholder="Message... (Enter sends; Shift+Enter adds a line break)", id="chat-input")
                    yield Button("Send", id="send-btn", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "gruvbox"
        if self.client is None:
            self.notify("Put your Gemini key in api_key.txt beside this script and restart.", severity="error", timeout=10)
        self.refresh_session_list()
        self.load_session_into_log(self.current)
        self.query_one("#chat-input", TextArea).focus()
        if self.client is not None:
            self.refresh_available_models()

    @work(exclusive=True)
    async def refresh_available_models(self) -> None:
        """Replace fallback choices with models enabled for this API key."""
        try:
            available = await asyncio.to_thread(self._list_available_models)
            if not available:
                return
            selector = self.query_one("#model-selector", Select)
            selector.set_options(available)
            self.selected_model = available[0][1]
            selector.value = self.selected_model
            self.sub_title = f"{self.sessions[self.current]['title']} · {self.selected_model}"
            self.notify(f"Loaded {len(available)} available Gemini models")
        except Exception as exc:
            self.notify(f"Could not load model list; using fallback choices: {exc}", severity="warning")

    def _list_available_models(self) -> list[tuple[str, str]]:
        models = []
        for model in self.client.models.list():
            name = str(getattr(model, "name", ""))
            model_id = name.removeprefix("models/")
            supported = getattr(model, "supported_actions", None) or getattr(model, "supported_generation_methods", None) or []
            if not model_id.startswith("gemini"):
                continue
            if supported and not any("generatecontent" in str(item).lower() for item in supported):
                continue
            excluded = ("embedding", "imagen", "veo", "aqa", "tts", "robotics", "computer-use")
            if any(word in model_id.lower() for word in excluded):
                continue
            display = getattr(model, "display_name", None) or model_id
            models.append((str(display), model_id))
        return sorted(set(models), key=lambda item: item[0].lower())

    @on(Select.Changed, "#model-selector")
    def model_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK and event.value:
            self.selected_model = str(event.value)
            self.sub_title = f"{self.sessions[self.current]['title']} · {self.selected_model}"

    # ── sidebar ──────────────────────────────────────────────

    def refresh_session_list(self) -> None:
        list_view = self.query_one("#session-list", ListView)
        list_view.clear()
        for i, sess in enumerate(self.sessions):
            list_view.append(SessionItem(i, sess["title"]))
        list_view.index = self.current

    def load_session_into_log(self, index: int) -> None:
        self.current = index
        sess = self.sessions[index]
        self.sub_title = f"{sess['title']} · {self.selected_model}"
        log = self.query_one("#chat-log", VerticalScroll)
        for child in list(log.children):
            child.remove()
        if not sess["log"]:
            log.mount(Static("Ask me anything to get started.", classes="empty-state"))
        for msg in sess["log"]:
            log.mount(MessageRow(MessageBubble(msg["role"], msg["text"])))
        log.scroll_end(animate=False)

    @on(ListView.Selected, "#session-list")
    def session_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, SessionItem) and item.session_index != self.current:
            self.load_session_into_log(item.session_index)
            self.query_one("#chat-input", TextArea).focus()

    @on(Button.Pressed, "#new-chat-btn")
    def new_chat_pressed(self) -> None:
        self.action_new_chat()

    def action_play_tetris(self) -> None:
        run_tetris()

    def action_toggle_theme(self) -> None:
        themes = list(self.available_themes.keys())
        current_index = themes.index(self.theme)
        next_index = (current_index + 1) % len(themes)
        self.theme = themes[next_index]
        self.notify(f"Theme: {self.theme}")

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display

    def action_cycle_chat(self) -> None:
        if len(self.sessions) < 2:
            return
        self.current = (self.current + 1) % len(self.sessions)
        self.load_session_into_log(self.current)
        self.refresh_session_list()
        self.notify(f"Chat: {self.sessions[self.current]['title']}")

    def action_new_chat(self) -> None:
        self.sessions.append(new_session())
        self.current = len(self.sessions) - 1
        save_sessions(self.sessions)
        self.refresh_session_list()
        self.load_session_into_log(self.current)
        self.query_one("#chat-input", TextArea).focus()

    def action_delete_chat(self) -> None:
        title = self.sessions[self.current]["title"]

        def handle_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            del self.sessions[self.current]
            if not self.sessions:
                self.sessions.append(new_session())
            self.current = min(self.current, len(self.sessions) - 1)
            save_sessions(self.sessions)
            self.refresh_session_list()
            self.load_session_into_log(self.current)

        self.push_screen(ConfirmScreen(f"Delete '{title}'? \nThis can't be undone."), handle_confirm)

    # ── message context menu actions ─────────────────────────

    def show_bubble_menu(self, bubble: MessageBubble, x: int, y: int) -> None:
        self.dismiss_bubble_menu()
        menu = ContextMenu()
        self._context_menu = menu
        self._context_menu_bubble = bubble
        self.screen.mount(menu)

        menu_width, menu_height = 26, 14  # matches ContextMenu's fixed CSS size
        # Anchor the menu's bottom-right corner to the cursor, so it opens
        # up and to the left, hovering over the chat log and background.
        menu.styles.offset = (max(0, x - menu_width), max(0, y - menu_height))

    def dismiss_bubble_menu(self) -> None:
        if self._context_menu is not None:
            self._context_menu.remove()
            self._context_menu = None
            self._context_menu_bubble = None

    def on_click(self, event: events.Click) -> None:
        if self._context_menu is None:
            return
        if event.widget is None or self._context_menu not in event.widget.ancestors_with_self:
            self.dismiss_bubble_menu()

    @on(Button.Pressed, ".menu-btn")
    def context_menu_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bubble = self._context_menu_bubble
        action = event.button.id
        self.dismiss_bubble_menu()
        if bubble is not None and action:
            self.handle_bubble_action(action, bubble)

    def handle_bubble_action(self, action: str, bubble: MessageBubble) -> None:
        chat_input = self.query_one("#chat-input", TextArea)
        snippet = bubble.full_text.strip()

        if action == "copy":
            self.copy_to_clipboard(snippet)
            self.notify("Copied to clipboard")
            return

        if action == "ask":
            chat_input.text = f"Explain or answer questions about this text:\n\n{snippet}"
        elif action == "summarize":
            chat_input.text = f"Summarize this text accurately and briefly:\n\n{snippet}"
        elif action == "source":
            chat_input.text = f"Find and list reliable sources for the claims in this text:\n\n{snippet}"
        else:
            return

        chat_input.focus()

    # ── sending messages ─────────────────────────────────────

    @on(Button.Pressed, "#send-btn")
    def send_pressed(self) -> None:
        self.send_message(self.query_one("#chat-input", TextArea).text)

    @on(Button.Pressed, ".copy-response")
    def copy_response_pressed(self, event: Button.Pressed) -> None:
        row = event.button.parent
        if isinstance(row, MessageRow):
            self.copy_to_clipboard(row.bubble.full_text)
            self.notify("Response copied to clipboard")

    @on(Button.Pressed, ".follow-up")
    def follow_up_pressed(self, event: Button.Pressed) -> None:
        self.query_one("#chat-input", TextArea).text = event.button.question
        self.query_one("#chat-input", TextArea).focus()

    def copy_to_clipboard(self, text: str) -> None:
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception:
            try:
                import subprocess
                subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
            except Exception as exc:
                self.notify(f"Clipboard unavailable: {exc}", severity="error")

    def send_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.client is None:
            self.notify("Gemini API key not found in api_key.txt.", severity="error")
            return
        chat_input = self.query_one("#chat-input", TextArea)
        chat_input.text = ""
        chat_input.focus()
        self.run_turn(text)

    @work(exclusive=True)
    async def run_turn(self, user_text: str) -> None:
        sess = self.sessions[self.current]
        log = self.query_one("#chat-log", VerticalScroll)
        ts = datetime.now().strftime("%H:%M")

        if not sess["log"]:
            sess["title"] = " ".join(user_text.split()[:5])[:30] or "New chat"

        ir = run_pipeline(user_text, INPUT_RULES)

        for empty_state in log.query(".empty-state"):
            empty_state.remove()
        await log.mount(MessageRow(MessageBubble("user", ir.text)))
        sess["log"].append({"role": "user", "text": ir.text, "ts": ts})
        log.scroll_end(animate=False)

        for v in ir.violations:
            if not v.blocked:
                self.notify(v.message, severity="warning")

        if ir.is_blocked:
            notice = f"[Blocked] {ir.violations[-1].message}"
            await log.mount(MessageRow(MessageBubble("system", notice)))
            sess["log"].append({"role": "system", "text": notice, "ts": datetime.now().strftime("%H:%M")})
            log.scroll_end(animate=False)
            save_sessions(self.sessions)
            self.refresh_session_list()
            return

        assistant_bubble = MessageBubble("assistant", "")
        await log.mount(MessageRow(assistant_bubble))
        log.scroll_end(animate=False)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + sess["history"] + [{"role": "user", "content": ir.text}]

        full_text = ""
        try:
            async for chunk in self.stream_completion(messages):
                full_text += chunk
                assistant_bubble.append_text(chunk)
                log.scroll_end(animate=False)
        except Exception as e:
            assistant_bubble.set_text(f"[Error contacting Gemini: {e}]")
            return

        out = run_pipeline(full_text, OUTPUT_RULES)
        if out.is_blocked:
            resp_text = "[Blocked] Output blocked by filters."
            assistant_bubble.set_text(resp_text)
        else:
            resp_text = out.text
            if resp_text != full_text:
                assistant_bubble.set_text(resp_text)
            sess["history"] = sess["history"] + [
                {"role": "user", "content": ir.text},
                {"role": "assistant", "content": resp_text},
            ]

        for v in out.violations:
            if not v.blocked:
                self.notify(v.message, severity="warning")

        sess["log"].append({"role": "assistant", "text": resp_text, "ts": datetime.now().strftime("%H:%M")})
        save_sessions(self.sessions)
        self.refresh_session_list()

        if resp_text and not resp_text.startswith("[Blocked]"):
            try:
                followups = await self.generate_followups(resp_text)
                for question in followups:
                    await log.mount(FollowUpBox(question))
                log.scroll_end(animate=False)
            except Exception:
                pass

    async def stream_completion(self, messages: list) -> AsyncIterator[str]:
        system_instruction = next(
            (message["content"] for message in messages if message["role"] == "system"),
            None,
        )
        contents = []
        for message in messages:
            if message["role"] == "system":
                continue
            content = message["content"]
            if message is messages[-1] and message["role"] == "user":
                link_context = await asyncio.to_thread(extract_link_context, content)
                if link_context:
                    content += "\n\nUse the following fetched source context. Cite the URL and distinguish it from your own reasoning:\n" + link_context
            contents.append(types.Content(
                role="user" if message["role"] == "user" else "model",
                parts=[types.Part.from_text(text=content)],
            ))
        stream = await self.client.aio.models.generate_content_stream(
            model=self.selected_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=MAX_COMPLETION_TOKENS,
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text

    async def generate_followups(self, response: str) -> list[str]:
        prompt = (
            "Based on the assistant response below, generate exactly three useful follow-up questions. "
            "Return one question per line, with no numbering or extra text.\n\n" + response[:6000]
        )
        result = await self.client.aio.models.generate_content(
            model=self.selected_model,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=256),
        )
        questions = [re.sub(r"^[-*\d.)\s]+", "", line).strip() for line in (result.text or "").splitlines()]
        return [q for q in questions if q][:3]


if __name__ == "__main__":
    ChatApp().run()
