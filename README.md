# Aira Desktop Assistant

Aira is a Python voice and text assistant for Windows. It can run common desktop tasks, keep local reminders and notes, and optionally answer general questions using the OpenAI API.

## Features

- Voice commands with the wake word **Aira**, plus text mode for keyboard input.
- Search the web and YouTube; open a YouTube result in the browser.
- Open built-in Windows apps and apps with an exact Start Menu shortcut.
- Search filenames and open supported files in the current Windows user's home folder.
- Set one-time local reminders and timers; save and read notes.
- Tell the time and date, calculate basic arithmetic, and report system information.
- Optional AI answers through the OpenAI API.

## Requirements

- Windows 10 or later.
- Python 3.10 or newer. Python 3.13 is a suitable choice for this project.
- A microphone and internet connection for voice recognition.

Voice recognition uses SpeechRecognition's Google Web Speech service, which sends recorded speech to Google for transcription. You can use text mode instead if you do not want to use the microphone.

## Setup

Open PowerShell in the project folder. Create a virtual environment and install the packages:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `py -3.13` is unavailable, install Python 3.13 or use another installed Python version 3.10 or newer with `py -m venv .venv`. If PyAudio installation fails, text mode can still be used; the microphone is only needed for voice mode.

## Run

Start voice mode:

```powershell
.\.venv\Scripts\python.exe .\AIRA.py
```

Start keyboard mode:

```powershell
.\.venv\Scripts\python.exe .\AIRA.py --text
```

In text mode, type `help` to see the built-in command examples. Press **Ctrl+C** to stop the assistant.

## Example commands

```text
aira, what time is it
remind me in 10 minutes to call Sam
remind me after 2 hours to take a break
remind me at 5 pm to take medicine
show reminders
set a timer for 5 minutes
search files for resume
open file report.docx
open app Notepad
play Arijit Singh on YouTube
calculate 12 divided by 3
remember buy milk
read notes
```

Reminders are one-time and stored locally in `%APPDATA%\Aira\reminders.json`. Notes are stored in `%APPDATA%\Aira\notes.json`. Aira needs to be running to announce a reminder at its scheduled time. A reminder that became due while Aira was closed is announced the next time Aira starts. This version does not sync with a calendar service or support recurring calendar events.

## Apps and files

File search looks for **filenames** under the current Windows user's home folder, including normal Desktop, Documents, Downloads, Pictures and OneDrive folders. Opening a file is limited to that home folder. Executable, script and shortcut file types are blocked. App commands open a built-in app or an exact matching Start Menu shortcut; arbitrary shell commands are not run.

## Optional OpenAI answers

Set an API key in the current PowerShell window before starting Aira:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
.\.venv\Scripts\python.exe .\AIRA.py
```

The key is read from the environment and is not stored in the script. Aira sends unmatched questions and the short conversation history to OpenAI when this option is enabled. Built-in commands work without an API key. The model can be changed with `OPENAI_MODEL` (default: `gpt-5-mini`).

## Android missed-call auto-reply

The Windows Python assistant cannot monitor calls on an Android phone directly. A phone-side automation app such as Tasker can trigger on a new missed call and send `I will call you later.` to the caller number. This is separate from Aira and depends on the phone allowing the required call and SMS permissions. Test with a number you control; unknown or hidden caller numbers may not be replyable. SMS charges may depend on the mobile plan.

## Privacy and safety

- The built-in file commands search filenames and open selected files; they do not upload file contents.
- Voice transcription uses Google's online speech-recognition service.
- Optional AI answers send the question and recent short conversation history to OpenAI.
- Do not commit API keys, `.env` files, virtual environments, or personal reminder/note data to GitHub.
