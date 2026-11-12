# vn-converter

Converts three specific visual novels, from a copy that **you own and have installed on a
Windows PC**, into one "bundle" file that the
[VN Player](https://mangoabuse.github.io/vn-player/) web app can play offline on an iPad
or in any current browser.

This repository contains **no game data**: no story text, pictures, music, voices or
video from any game. It is only a program that reads the files of a game already
installed on your own PC.

It is a personal hobby project, shared as it is. Please read the next section before
using it.

## Before you use it

- **Use it only with games you own.** You need your own legally obtained, installed copy
  of each game. The converter downloads nothing. It does not touch the game's program
  or its copy protection; it only reads the data files the installed game already uses.
- **A bundle is the game.** The file the converter produces holds the game's complete
  story, pictures, music and voices in another format. Keep it on your own devices. Do
  not upload it, host it, or pass it to anyone: that would be distributing the game.
- **It is meant for personal use only**: reading a game you own on your own tablet.
  Whether making such a copy is permitted where you live is for you to check.
- **No affiliation.** This project is not connected with, or endorsed by, the developers
  or publishers of these games. The game titles are trademarks of their owners.
- **No support.** It was written for, and tested on, one PC with the exact releases
  listed below. Issues are switched off. If your release differs, it may not work.

## Supported games

Only these three, and only in the releases named. Other games, other language editions,
console versions, and earlier or later builds are untested and probably will not work,
even when they use the same engine.

| Game | Release it was tested with | Name on the command line | Its folder contains |
|---|---|---|---|
| **Saya no Uta ~ The Song of Saya** | JAST USA English release for Windows (game files dated 2013) | `saya` | `saya.exe`, `cg.npa`, `nss.npa`, `sound.npa`, `voice.npa` |
| **Little Busters! English Edition** | Windows release, build of November 2017 (program version 1.0.0.0) | `lb` | `LITBUS_WIN32.exe` and a `files` folder with `SCRIPT.PAK` and the other `.PAK` archives |
| **Wonderful Everyday ~Down the Rabbit-Hole~** | Steam release (Ethornell/BGI engine 1.643.6) with the publisher's free full-game patch ver 2.0 and the fan-made voice patch V2 installed | `we` | `BGI.exe` and the `data0....arc` archives |

### What you get, and what is simplified

Every bundle has the story text, speaker names, backgrounds, character pictures, event
pictures, music, sound effects, voices and choices. The player adds its own text box,
backlog, save slots, auto mode and skip mode. The original games' own menus (title
screen, gallery, music room, scene replay, settings) are **not** carried over.

- **Saya no Uta**: complete story including the endings and staff roll.
- **Little Busters!**: the mini-games (baseball practice, the ranking battles and so on)
  cannot be played. Battles are decided at random and the story carries on; a few screen
  effects are dropped. The movies the story plays are included.
- **Wonderful Everyday**: patterned screen wipes become plain crossfades, picture pans
  and most screen effects are dropped, and scenes timed to music use approximate timing.
  The chapter menu is included and only offers the chapters you have unlocked. Four
  movies are included.

Each bundle was checked by playing its opening on an iPad. Later parts of the games have
had little or no testing.

## What you need

- Windows 10 or 11
- [Python](https://www.python.org/downloads/) 3.12 or newer, with Pillow:
  `python -m pip install Pillow`
- [ffmpeg](https://ffmpeg.org/) on the PATH, for example:
  `winget install --id Gyan.FFmpeg -e --source winget`
- Several GB of free disk space for the bundle and the conversion cache
- The game, installed and working on the same PC

## How to convert a game

1. Download this repository (the green **Code** button, then **Download ZIP**) and
   unpack it somewhere, for example `C:\vn-converter`.
2. Open a terminal in its `converter` folder.
3. Run the line for your game. `--game` is the folder that contains the game's program
   file (see the table above); put it in quotes.

   ```
   python convert.py saya --game "C:\Games\Saya no Uta"
   python convert.py lb   --game "C:\Games\Little Busters English Edition"
   python convert.py we   --game "C:\Program Files (x86)\Steam\steamapps\common\Wonderful Everyday Down the Rabbit-Hole"
   ```

4. Wait. The first run converts every picture and sound: a few minutes for Saya no Uta,
   roughly 20 to 40 minutes for the other two, depending on your PC.
5. The bundle appears in the `out` folder next to `converter`:

   | Game | File | Approximate size |
   |---|---|---|
   | Saya no Uta | `saya-no-uta.vnbundle.zip` | 95 MB |
   | Little Busters! | `little-busters-ee.vnbundle.zip` | 835 MB |
   | Wonderful Everyday | `wonderful-everyday.vnbundle.zip` | 1.1 GB with both patches |

The converter ends with a short report: how many scenes, text lines and media files it
found, anything it could not find, and any warnings. The game folder is only read, never
changed.

Other things worth knowing:

- Converted media is cached in the `work` folder, so running the converter again only
  redoes what changed. You can delete `work` at any time.
- `python convert.py lb --game "..." --scripts-only` (also for `we`) translates the
  scripts and prints the report without converting any media. It takes seconds and is a
  quick way to see whether your release is understood.

### Patches (Wonderful Everyday)

The game is converted exactly as it is installed. Install your patches into the game
folder first, following their own instructions, check that the game runs on the PC, and
then convert. The converter looks for each file where the game does: first in the loose
folders `Script\CVTD`, `Graphic\CVTD`, `Sound\CVTD` and `Voice\CVTD`, then among loose
files in the game folder, then in the archives, where a later archive wins over an
earlier one. To add or remove a patch later, change the game folder, convert again and
import the new bundle.

The unpatched Steam release has not been tested.

## Getting a bundle onto an iPad

The bundle travels from your PC to your iPad over your own home network. It is never
uploaded anywhere.

1. **Install the player.** On the iPad, open
   <https://mangoabuse.github.io/vn-player/> in Safari, tap **Share**, then **Add to
   Home Screen**. From then on, open **VN Player** from the Home Screen icon: the icon
   and Safari keep separate storage.
2. **Keep downloads off iCloud.** In **Settings**, **Apps**, **Safari**, **Downloads**,
   choose **On My iPad**. Otherwise the downloaded bundle is uploaded to iCloud Drive.
3. **Share the bundle on your home network.** On the PC run

   ```
   python tools\share_bundles.py
   ```

   and, if Windows Firewall asks, allow it for **private** networks only. It prints an
   address such as `http://192.168.1.23:8080/`. While it runs, anyone on the same
   network can download your bundles, so use it only at home and close its window (or
   press Ctrl+C) as soon as you are done.
4. **Download.** In Safari on the iPad, open that address, tap the bundle, then tap
   **Download**.
5. **Import.** In VN Player, tap **Import a game bundle**, browse to **On My iPad**,
   **Downloads**, and pick the file. Keep the app open until the progress bar finishes.
6. **Tidy up.** Delete the downloaded zip in the Files app and empty Recently Deleted.
   Until you do, the game takes up twice its size on the iPad.

Without Wi-Fi you can copy the bundle to a USB drive instead and plug that into the
iPad; it shows up in the Files app.

The player keeps the game inside its own storage on the device and sends nothing
anywhere. It works without a connection once it has been opened once. Its source is at
<https://github.com/MangoAbuse/vn-player>; you can also host your own copy on any static
HTTPS site. It was tested on an iPad with iPadOS 17 or later and in a current desktop
browser.

## What is in this repository

| File | Purpose |
|---|---|
| `converter/convert.py` | Command line entry point |
| `converter/bundle.py` | The bundle format (documented at the top of the file) and the zip writer |
| `converter/media.py` | Picture to WebP and sound to AAC conversion, with a cache |
| `converter/npa.py`, `nss.py`, `saya.py` | Saya no Uta: archive reader, script parser, translator |
| `converter/luca.py`, `littlebusters.py` | Little Busters!: archive, picture and script readers, translator |
| `converter/bgi.py`, `bgi_script.py`, `wonderful.py` | Wonderful Everyday: archive, picture and sound readers, script disassembler, translator |
| `tools/share_bundles.py` | Offers the bundles in `out` for download on your home network |

## Licence and credits

Copyright (C) 2026 MangoAbuse. Written with the help of Claude, an AI assistant.

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE).

The licence covers this program only. It gives you no rights to any game.

The file formats were learned from these projects, with thanks:

- [LuckSystem](https://github.com/wetor/LuckSystem) (MIT): Little Busters! archive,
  picture and script layouts, and its opcode list. Its licence notice is in
  `converter/luca.py`.
- [lbee-utils](https://github.com/G2-Games/lbee-utils) (GPLv3): Little Busters! picture
  and archive layouts.
- [GARbro](https://github.com/morkt/GARbro) (MIT, Copyright (C) 2015-2018 by morkt): the
  Ethornell compression and picture formats. Its licence notice is in
  `converter/bgi.py`.
- [arc-reader](https://github.com/sgipo/arc-reader) and
  [EthornellTools](https://github.com/arcusmaximus/EthornellTools) (MIT): Ethornell
  archive and script notes.
- [nipa](https://github.com/Wilhansen/nipa): its description of the Nitroplus archive
  layout.
