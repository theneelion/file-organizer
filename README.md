# File Organizer

A simple Windows utility that automatically sorts files into folders based on their type.

## What It Does

Drop the organizer into any folder you want to clean up and run it.

For example:

```text
My Folder/
├── photo.jpg
├── song.mp3
├── movie.mp4
├── report.pdf
├── setup.exe
├── main.py
└── user32.dll
```

After running:

```text
My Folder/
├── Audio/
│   └── Audio_1.mp3
├── Image/
│   └── Image_1.jpg
├── Video/
│   └── Video_1.mp4
├── PDF/
│   └── PDF_1.pdf
├── Program/
│   ├── user32.dll
│   └── main.py
├── Applications/
│   └── setup.exe
├── Organizer.py
├── Organize Files.bat
└── config.json
```

## How to Use

### 1. Copy these files into the folder you want to organize

```text
Organizer.py
Organize Files.bat
config.json
```

### 2. Double-click `Organize Files.bat`

That's it.

The organizer works on the folder containing the BAT file, so you can move the three files to another folder and use them there too.

## File Naming

Most categories rename files into a simple numbered format:

```text
Audio_1.mp3
Audio_2.wav
Image_1.jpg
PDF_1.pdf
```

The original file extension is kept.

Some categories can preserve the original filename instead.

For example, files in **Program & Applications** are moved without renaming:

```text
Program/
├── main.py
├── project.cpp
└── driver.sys

Applications/
└── setup.exe
```

This is controlled in `config.json`.

## Customize It

You can change the categories, extensions, and filename behavior in `config.json`.

Each category supports:

```json
{
  "rename": true,
  "extensions": [".mp3", ".wav"],
  "mime_types": ["audio/*"]
}
```

`rename` controls what happens after a file is classified:

```text
true  → move + rename
false → move + keep original filename
```

For example:

```json
"Program": {
  "rename": false,
  "extensions": [".py", ".cpp", ".dll"],
  "mime_types": []
}
```

## Unknown Files

If a file type is not recognized, the organizer first tries MIME-type detection.

Unrecognized files are placed in:

```text
Unknown/
```

Files without an extension trigger a warning. You can choose whether to continue with MIME detection or skip the file.

## Safe by Design

The organizer:

- Does not overwrite existing files.
- Does not scan subfolders recursively.
- Does not modify the organizer's own files.
- Skips symbolic links and Windows `.lnk` shortcuts.
- Keeps permanent numbering for automatically renamed files.

## Requirements

- Windows
- Python 3

No third-party Python packages are required.

## Files

```text
Organizer.py          ← main program
Organize Files.bat    ← double-click to run
config.json           ← file categories and rules
```

The organizer creates its own small runtime files when needed.

## Important

The organizer **moves** files rather than copying them.

When using `"rename": false`, the original filename is preserved, but moving a program, library, or system-related file can still affect software that expects it to remain in its original location.
