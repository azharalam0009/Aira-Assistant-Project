"""Aira — an extensible, voice-controlled desktop assistant.

Voice mode uses SpeechRecognition's Google Web Speech recognizer, so it needs
an internet connection. Text mode (``python aira.py --text``) works without
a microphone. OpenAI chat is optional and is enabled with OPENAI_API_KEY.

Install optional dependencies with:
    python -m pip install pyttsx3 SpeechRecognition PyAudio openai yt-dlp
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import logging
import operator
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path


try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


APP_NAME = "Aira"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
LANGUAGE = os.environ.get("AIRA_LANGUAGE", "en-IN")
WAKE_WORD = os.environ.get("AIRA_WAKE_WORD", "aira").strip().lower() or "aira"


def get_data_directory() -> Path:
    """Return a per-user data folder for Aira's notes."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    folder = base / "Aira"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class AiraAssistant:
    def __init__(self, text_mode: bool = False) -> None:
        self.text_mode = text_mode
        self.wake_word = WAKE_WORD
        self.language = LANGUAGE
        self.speech_rate = int(os.environ.get("AIRA_SPEECH_RATE", "175"))
        self._speech_lock = threading.Lock()
        self._calibrated = False
        self._recognizer = sr.Recognizer() if sr is not None else None
        self._microphone_failed = False
        if self._recognizer:
            self._recognizer.dynamic_energy_threshold = True
            self._recognizer.pause_threshold = 0.75

        self._engine = None
        if pyttsx3 is not None:
            try:
                self._engine = pyttsx3.init()
                self._engine.setProperty("rate", self.speech_rate)
                self._engine.setProperty("volume", 1.0)
            except Exception as exc:
                logging.warning("Text-to-speech could not be initialized: %s", exc)

        self._notes_file = get_data_directory() / "notes.json"
        self._reminders_file = get_data_directory() / "reminders.json"
        self._reminders_lock = threading.Lock()
        self._conversation: list[dict[str, str]] = []
        self._ai_client = None
        if OpenAI is not None and os.environ.get("OPENAI_API_KEY"):
            try:
                self._ai_client = OpenAI()
            except Exception as exc:
                logging.warning("OpenAI client could not be initialized: %s", exc)

        self._reminder_thread = threading.Thread(target=self._reminder_worker, daemon=True)
        self._reminder_thread.start()

    def speak(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        print(f"{APP_NAME}: {text}")
        if self._engine is not None:
            try:
                with self._speech_lock:
                    self._engine.say(text)
                    self._engine.runAndWait()
            except Exception as exc:
                logging.warning("Speech output failed: %s", exc)

    def listen_once(self) -> str:
        """Listen for one phrase and return recognized text, or an empty string."""
        if sr is None or self._recognizer is None:
            self.speak("Voice recognition is unavailable. Install SpeechRecognition and PyAudio, or run Aira with --text.")
            return ""
        try:
            device_value = os.environ.get("AIRA_MICROPHONE")
            device_index = int(device_value) if device_value else None
            with sr.Microphone(device_index=device_index) as source:
                if not self._calibrated:
                    print("Calibrating microphone…")
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.6)
                    self._calibrated = True
                print("Listening…")
                audio = self._recognizer.listen(source, timeout=6, phrase_time_limit=9)
            text = self._recognizer.recognize_google(audio, language=self.language).strip()
            if text:
                print(f"You: {text}")
            return text.lower()
        except sr.WaitTimeoutError:
            return ""
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as exc:
            logging.warning("Speech recognition service failed: %s", exc)
            self.speak("Speech recognition is unavailable right now. Check your internet connection.")
            return ""
        except (AttributeError, OSError, ValueError) as exc:
            self._microphone_failed = True
            logging.error("Microphone could not be opened: %s", exc)
            self.speak("I could not access the microphone. Check the device and AIRA_MICROPHONE setting.")
            return ""

    def _take_wake_command(self, phrase: str) -> tuple[bool, str]:
        # Speech recognition may transcribe the name Aira as a common spelling
        # variant. Accept those variants while keeping Aira as the spoken name.
        wake_words = {self.wake_word}
        if self.wake_word == "aira":
            wake_words.update({"ayra", "ira", "eyra"})
        matches = [
            match
            for wake_word in wake_words
            if (match := re.search(rf"\b{re.escape(wake_word)}\b", phrase, flags=re.IGNORECASE))
        ]
        match = min(matches, key=lambda item: item.start()) if matches else None
        if not match:
            return False, ""
        remaining = (phrase[:match.start()] + " " + phrase[match.end():]).strip(" ,.!?")
        return True, remaining

    @staticmethod
    def _looks_like_builtin_command(phrase: str) -> bool:
        """Allow clear built-in commands even when speech misses the wake word."""
        return bool(re.search(
            r"\b(open|play|search|google search|calculate|timer|note|remember|"
            r"read notes|show notes|remind|reminders|schedule|find file|files|"
            r"time|clock|date|today|day|hello|hi|hey|"
            r"help|commands|what can you do|weather|system information|"
            r"system status|about this computer|exit|quit|goodbye)\b",
            phrase,
        ))

    def run_voice(self) -> None:
        if sr is None:
            self.speak("Voice mode needs SpeechRecognition and PyAudio. Run with --text for keyboard mode.")
            return
        self.speak(f"{APP_NAME} is ready. Say {self.wake_word}, or give a command.")
        while True:
            phrase = self.listen_once()
            if self._microphone_failed:
                self.speak("You can restart Aira with --text to use keyboard mode.")
                return
            if not phrase:
                continue
            woke, command = self._take_wake_command(phrase)
            if not woke:
                if not self._looks_like_builtin_command(phrase):
                    continue
                command = phrase
            else:
                self.speak("I am listening.")
                if not command:
                    command = self.listen_once()
            if command and not self.handle_command(command):
                break

    def run_text(self) -> None:
        self.speak("Text mode is ready. Type help to see commands.")
        while True:
            try:
                command = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if command and not self.handle_command(command.lower()):
                break

    def _open_search(self, query: str, service: str = "google") -> None:
        query = query.strip()
        if not query:
            self.speak("Tell me what you would like me to search for.")
            return
        encoded = urllib.parse.quote_plus(query)
        if service == "youtube":
            url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            url = f"https://www.google.com/search?q={encoded}"
        webbrowser.open(url)
        self.speak(f"Searching {service.title()} for {query}.")

    def _reminder_worker(self) -> None:
        """Fire due reminders while Aira is running; reminder data persists across restarts."""
        while True:
            due: list[dict[str, str]] = []
            now = dt.datetime.now().astimezone()
            with self._reminders_lock:
                reminders = self._read_reminders_unlocked()
                pending = []
                for reminder in reminders:
                    try:
                        due_at = dt.datetime.fromisoformat(reminder["at"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if due_at <= now:
                        due.append(reminder)
                    else:
                        pending.append(reminder)
                if len(pending) != len(reminders):
                    self._write_reminders_unlocked(pending)
            for reminder in due:
                self.speak(f"Reminder: {reminder.get('text', 'Your reminder is due.')}")
            time.sleep(5)

    def _read_reminders_unlocked(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self._reminders_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _write_reminders_unlocked(self, reminders: list[dict[str, str]]) -> None:
        try:
            self._reminders_file.write_text(json.dumps(reminders, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logging.warning("Could not save reminders: %s", exc)

    def _add_reminder(self, query: str) -> None:
        now = dt.datetime.now().astimezone()
        relative = re.search(
            r"\bremind me (?:in|after)\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\s+(?:to\s+)?(.+)$",
            query,
        )
        if relative:
            amount = int(relative.group(1))
            unit = relative.group(2)
            seconds = amount * (86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
            if not 1 <= seconds <= 365 * 86400:
                self.speak("Reminder time must be between one second and one year.")
                return
            due_at = now + dt.timedelta(seconds=seconds)
            reminder_text = relative.group(3).strip(" .")
        else:
            absolute = re.search(
                r"\bremind me at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+(?:to\s+)?(.+)$",
                query,
            )
            if not absolute:
                self.speak("Say, remind me in 10 minutes to call Sam, or remind me at 5 pm to call Sam.")
                return
            hour = int(absolute.group(1)) % 12 + (12 if absolute.group(3) == "pm" else 0)
            minute = int(absolute.group(2) or 0)
            if minute > 59:
                self.speak("That reminder time is not valid.")
                return
            due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due_at <= now:
                due_at += dt.timedelta(days=1)
            reminder_text = absolute.group(4).strip(" .")

        if not reminder_text:
            self.speak("What should I remind you about?")
            return
        record = {"at": due_at.isoformat(timespec="seconds"), "text": reminder_text}
        with self._reminders_lock:
            reminders = self._read_reminders_unlocked()
            reminders.append(record)
            self._write_reminders_unlocked(reminders)
        self.speak(f"Reminder set for {due_at.strftime('%I:%M %p')}: {reminder_text}.")

    def _list_reminders(self) -> None:
        with self._reminders_lock:
            reminders = self._read_reminders_unlocked()
        if not reminders:
            self.speak("You do not have any upcoming reminders.")
            return
        now = dt.datetime.now().astimezone()
        upcoming = sorted(reminders, key=lambda item: item.get("at", ""))[:5]
        for reminder in upcoming:
            print(f"Reminder: {reminder.get('at', '')} — {reminder.get('text', '')}")
        self.speak("Upcoming reminders: " + ". ".join(
            f"{dt.datetime.fromisoformat(item['at']).astimezone().strftime('%I:%M %p')}, {item.get('text', '')}"
            for item in upcoming
        ))

    def _search_user_files(self, term: str, limit: int = 5) -> list[Path]:
        """Search filenames under the current user's home folder, skipping bulky caches."""
        home = Path.home()
        needle = term.casefold().strip()
        if not needle:
            return []
        skipped = {"appdata", ".venv", "venv", "node_modules", ".git", "__pycache__", "windowsapps"}
        matches: list[Path] = []
        visited = 0
        for root, dirs, files in os.walk(home, onerror=lambda _error: None):
            dirs[:] = [name for name in dirs if name.casefold() not in skipped and not name.startswith(".")]
            visited += 1
            if visited > 25000:
                break
            for filename in files:
                if needle in filename.casefold():
                    matches.append(Path(root) / filename)
                    if len(matches) >= limit:
                        return matches
        return matches

    def _find_user_files(self, term: str) -> list[Path]:
        self.speak(f"Searching your personal files for {term}.")
        matches = self._search_user_files(term)
        if not matches:
            self.speak("I could not find a matching file in your user folders.")
            return []
        for path in matches:
            print(path)
        self.speak(f"I found {len(matches)} matching file{'' if len(matches) == 1 else 's'}. The paths are shown in the terminal.")
        return matches

    def _open_user_file(self, name_or_path: str) -> None:
        blocked_extensions = {
            ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".msi", ".scr",
            ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".py",
            ".pyw", ".sh", ".lnk", ".reg", ".scf",
        }
        candidate = Path(name_or_path.strip().strip('"')).expanduser()
        if not candidate.is_absolute():
            matches = self._search_user_files(candidate.name)
            if not matches:
                self.speak("I could not find that file in your user folders.")
                return
            candidate = matches[0]
        try:
            candidate = candidate.resolve(strict=True)
            candidate.relative_to(Path.home().resolve())
        except (OSError, ValueError):
            self.speak("I can open files from your user folder only.")
            return
        if not candidate.is_file() or candidate.suffix.casefold() in blocked_extensions:
            self.speak("That file type cannot be opened by voice command.")
            return
        try:
            if os.name == "nt":
                os.startfile(str(candidate))
            else:
                webbrowser.open(candidate.as_uri())
            self.speak(f"Opening {candidate.name}.")
        except OSError as exc:
            logging.warning("Could not open file %s: %s", candidate, exc)
            self.speak("I could not open that file.")

    def _play_youtube(self, query: str) -> None:
        """Find the top YouTube match and open its watch page for playback."""
        query = re.sub(r"\s+on\s+youtube\s*$", "", query.strip(), flags=re.IGNORECASE)
        if not query:
            self.speak("Tell me which song you want to play.")
            return
        if yt_dlp is None:
            self.speak("I need the yt-dlp package to open a matching video. Opening YouTube search for now.")
            self._open_search(query, "youtube")
            return

        try:
            options = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
            with yt_dlp.YoutubeDL(options) as ydl:
                search = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = (search or {}).get("entries") or []
            first = next((entry for entry in entries if entry), None)
            if not first:
                raise LookupError("No YouTube result found")

            video_url = first.get("webpage_url")
            if not video_url and first.get("id"):
                video_url = f"https://www.youtube.com/watch?v={first['id']}"
            if not video_url:
                raise LookupError("YouTube result did not include a video URL")

            separator = "&" if "?" in video_url else "?"
            webbrowser.open(f"{video_url}{separator}autoplay=1")
            title = first.get("title") or query
            self.speak(f"Opening {title} on YouTube.")
        except Exception as exc:
            logging.warning("Could not find a YouTube video: %s", exc)
            self.speak("I could not find a direct video, so I am opening YouTube search.")
            self._open_search(query, "youtube")

    def _load_notes(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self._notes_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Could not read notes: %s", exc)
            return []

    def _save_note(self, note: str) -> None:
        notes = self._load_notes()
        notes.append({"created": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "text": note})
        try:
            self._notes_file.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
            self.speak("Note saved.")
        except OSError as exc:
            logging.error("Could not save note: %s", exc)
            self.speak("I could not save that note.")

    @staticmethod
    def _calculate(expression: str) -> float | int:
        """Evaluate basic arithmetic without eval or arbitrary code execution."""
        expression = expression[:100].lower().strip()
        for phrase, symbol in (("to the power of", "**"), ("divided by", "/"),
                               ("multiplied by", "*"), ("times", "*"),
                               ("plus", "+"), ("minus", "-")):
            expression = expression.replace(phrase, symbol)
        tree = ast.parse(expression, mode="eval")
        binary_ops = {
            ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod, ast.Pow: operator.pow,
        }
        unary_ops = {ast.UAdd: operator.pos, ast.USub: operator.neg}

        def visit(node: ast.AST) -> float | int:
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                if abs(node.value) > 1e12:
                    raise ValueError("number is too large")
                return node.value
            if isinstance(node, ast.UnaryOp) and type(node.op) in unary_ops:
                return unary_ops[type(node.op)](visit(node.operand))
            if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
                left, right = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 10:
                    raise ValueError("power is too large")
                result = binary_ops[type(node.op)](left, right)
                if isinstance(result, (int, float)) and abs(result) > 1e100:
                    raise ValueError("result is too large")
                return result
            raise ValueError("unsupported expression")

        return visit(tree)

    def _ask_ai(self, prompt: str) -> str:
        if self._ai_client is None:
            return "AI chat is off. Install the openai package and set OPENAI_API_KEY to enable it."
        try:
            response = self._ai_client.responses.create(
                model=DEFAULT_MODEL,
                instructions=("You are Aira, a concise, friendly desktop voice assistant. "
                              "Answer naturally and keep spoken replies brief."),
                input=self._conversation + [{"role": "user", "content": prompt}],
                store=False,
            )
            answer = (response.output_text or "").strip()
            if answer:
                self._conversation.extend([
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer},
                ])
                self._conversation = self._conversation[-12:]
                return answer
            return "I did not get a response. Please try again."
        except Exception as exc:
            logging.warning("AI request failed: %s", exc)
            return "I could not reach the AI service. Check your key, model setting, and internet connection."

    def _start_timer(self, query: str) -> None:
        match = re.search(r"\btimer\b.*?\b(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", query)
        if not match:
            self.speak("Try saying, set a timer for 5 minutes.")
            return
        amount = int(match.group(1))
        unit = match.group(2)
        multiplier = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        seconds = amount * multiplier
        if not 1 <= seconds <= 86400:
            self.speak("Timers must be between one second and 24 hours.")
            return
        timer = threading.Timer(seconds, self.speak, args=("Your timer is done.",))
        timer.daemon = True
        timer.start()
        self.speak(f"Timer set for {amount} {unit}.")

    def handle_command(self, raw_query: str) -> bool:
        """Handle one command. Return False when Aira should stop."""
        query = re.sub(r"\s+", " ", raw_query.lower()).strip(" .,!?\t")
        if not query:
            return True

        if re.search(r"\b(exit|quit|goodbye|shut down aira)\b", query):
            self.speak("Goodbye.")
            return False

        if re.search(r"\b(help|what can you do|commands)\b", query):
            self.speak("Try: remind me in 10 minutes to call Sam; remind me at 5 pm to take medicine; show reminders; search files for resume; open file report; open app Notepad; or play a song on YouTube. I can also calculate, tell time and date, save notes, set timers, search the web, and answer with optional AI.")
        elif query in {"reminders", "list reminders", "show reminders", "my schedule", "show my schedule"}:
            self._list_reminders()
        elif query.startswith("remind me "):
            self._add_reminder(query)
        elif re.search(r"\b(set|start|create)\s+(?:a\s+)?timer\b|\btimer\b", query):
            self._start_timer(query)
        elif re.search(r"\b(time|clock)\b", query):
            self.speak(f"The time is {dt.datetime.now().astimezone().strftime('%I:%M %p')}.")
        elif re.search(r"\b(date|today|day)\b", query):
            self.speak(f"Today is {dt.datetime.now().astimezone().strftime('%A, %d %B %Y')}.")
        elif re.search(r"\b(hello|hi|hey)\b", query):
            self.speak("Hello! What can I do for you?")
        elif "read notes" in query or "show notes" in query:
            notes = self._load_notes()
            if not notes:
                self.speak("You do not have any saved notes yet.")
            else:
                self.speak("Your latest notes are: " + ". ".join(item.get("text", "") for item in notes[-5:]))
        elif query.startswith("search files for "):
            self._find_user_files(query.removeprefix("search files for ").strip())
        elif query.startswith("find file "):
            self._find_user_files(query.removeprefix("find file ").strip())
        elif query.startswith("open file "):
            self._open_user_file(query.removeprefix("open file ").strip())
        elif query.startswith("open app "):
            self._open_desktop_app(query.removeprefix("open app ").strip())
        elif re.search(r"\b(note|remember)\b", query):
            note = re.sub(r"^(?:please\s+)?(?:take\s+a\s+note|make\s+a\s+note|save\s+a\s+note|note|remember)\b\s*(?:that\s+)?", "", query).strip(" .,:;")
            if note:
                self._save_note(note)
            else:
                self.speak("What would you like me to remember?")
        elif query.startswith("calculate ") or query.startswith("what is ") and any(op in query for op in (" plus ", " minus ", " times ", " divided by ")):
            expression = re.sub(r"^(?:calculate|what is)\s+", "", query)
            try:
                result = self._calculate(expression)
                self.speak(f"The answer is {result}.")
            except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
                logging.info("Calculation rejected: %s", exc)
                self.speak("I could not calculate that. Use a simple numeric expression, like 12 divided by 3.")
        elif "weather" in query:
            location = re.sub(r".*\bweather\b(?:\s+in)?\s*", "", query).strip()
            self._open_search(f"weather {location}".strip())
        elif query.startswith("search youtube for "):
            self._open_search(query.removeprefix("search youtube for "), "youtube")
        elif query.startswith("google search "):
            self._open_search(query.removeprefix("google search ").removeprefix("for "))
        elif query.startswith("search for "):
            self._open_search(query.removeprefix("search for "))
        elif query.startswith("play "):
            self._play_youtube(query.removeprefix("play "))
        elif query.startswith("open "):
            target = query.removeprefix("open ").strip()
            sites = {
                "youtube": "https://www.youtube.com", "google": "https://www.google.com",
                "facebook": "https://www.facebook.com", "linkedin": "https://www.linkedin.com",
                "github": "https://github.com", "gmail": "https://mail.google.com",
                "wikipedia": "https://www.wikipedia.org",
            }
            site = next((name for name in sites if name in target), None)
            if site:
                webbrowser.open(sites[site])
                self.speak(f"Opening {site.title()}.")
            elif target in {"calculator", "notepad"}:
                self._open_desktop_app(target)
            else:
                self._open_search(target)
        elif "system information" in query or "system status" in query or "about this computer" in query:
            self.speak(f"You are running {platform.system()} {platform.release()}, Python {platform.python_version()}.")
        else:
            self.speak(self._ask_ai(raw_query))
        return True

    def _open_desktop_app(self, app_name: str) -> None:
        """Open a built-in app or an exact Start Menu shortcut without using a shell."""
        app_name = app_name.strip().strip('"')
        if not app_name or Path(app_name).name != app_name:
            self.speak("Tell me the app name, for example, open app Notepad.")
            return
        commands = {
            "Windows": {
                "calculator": ["calc.exe"], "notepad": ["notepad.exe"],
                "paint": ["mspaint.exe"], "explorer": ["explorer.exe"],
                "task manager": ["taskmgr.exe"],
            },
            "Darwin": {"calculator": ["open", "-a", "Calculator"], "notepad": ["open", "-a", "TextEdit"]},
            "Linux": {"calculator": ["gnome-calculator"], "notepad": ["gedit"]},
        }
        command = commands.get(platform.system(), {}).get(app_name)
        try:
            if command:
                executable = command[0]
                if platform.system() == "Linux" and shutil.which(executable) is None:
                    self.speak(f"{app_name.title()} is not installed or is not on PATH.")
                    return
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.name == "nt":
                shortcut_roots = [
                    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                    Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                ]
                shortcut = next((
                    item
                    for root in shortcut_roots if root.is_dir()
                    for item in root.rglob("*.lnk")
                    if item.stem.casefold() == app_name.casefold()
                ), None)
                if shortcut is None:
                    self.speak(f"I could not find an app shortcut named {app_name} in the Start Menu.")
                    return
                os.startfile(str(shortcut))
            else:
                self.speak(f"I do not know how to open {app_name} on this system.")
                return
            self.speak(f"Opening {app_name}.")
        except OSError as exc:
            logging.warning("Could not open %s: %s", app_name, exc)
            self.speak(f"I could not open {app_name}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Aira voice assistant")
    parser.add_argument("--text", action="store_true", help="use keyboard input instead of a microphone")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    assistant = AiraAssistant(text_mode=args.text)
    try:
        assistant.run_text() if args.text else assistant.run_voice()
    except KeyboardInterrupt:
        print()
        assistant.speak("Goodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

