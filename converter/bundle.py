"""Bundle format shared by the converter and the web player.

A bundle is one .zip file:

    manifest.json            game description (see below)
    script/<scene>.json      {"id": "...", "ops": [ ... ]}
    img/...                  .webp images
    audio/bgm|se|voice/...   .m4a (AAC) audio
    video/...                .mp4 (H.264/AAC), only if the game has movies

Media entries are stored without zip compression (they are already compressed),
so the player can copy them out with plain byte-range reads. JSON is deflated.

manifest.json
    format    1
    id, title
    screen    {"w": 800, "h": 600}          coordinate space used by the script
    start     scene id to run for a new game
    scenes    [scene ids]
    boxes     {name: {"x","y","w","h", "bg": image or null, "bgx","bgy", "style": "nvl"|"adv"}}
    font      {"size", "lineHeight", "color", "outline"}
    slots     {"L": x-centre, ...}          sprite positions (bottom aligned)
    bgm       {id: {"src": file, "loop": [startMs, endMs] or null, "ms": duration}}
    names     {speaker id: display name}
    ui        optional images for title screen and choice buttons
    vars      numbered-variable games: {"argBase": first argument variable of `call`,
              "locals": [lo, hi] saved and restored around `call`,
              "globals": [[lo, hi], ...] kept across saves and new games}
    minigames optional hints for `ext` commands that drive minigames the player cannot run:
              "rush": [ext names]; after one of them, timed waits are dropped until the next text
              "resolve": [{"name", "codes": [first value of the command], "zeroOneOf": [variables]}]
                         when such a command runs and none of the variables is 0 yet, one of
                         them (picked at random) is set to 0, which lets the script's own
                         "someone lost" ending take over
    stats     {"files", "bytes"}

Script ops (every op is {"op": name, ...}); times are milliseconds, volumes and
alphas are 0..1, colours are "#rrggbb":

  Screen. Changes are staged behind a snapshot of the current picture and only
  become visible when a `wipe` runs, exactly like the original engines.
    clear                                    stage an empty screen (also hides the text box)
    bg      {src | color}                    background; neither = remove
            alt: [{"if": expr, "src": file}] alternative image when expr is true
            blurIf: expr                     soften the image when expr is true
    layer   {n, src | color, alpha, x, y}    overlay layer n (0 below sprites, 1-3 above)
    sprite  {slot, src}                      character sprite in a slot; src null = remove
    move    {target, ms, x, y, wait}         target "bg" | "layerN"; x/y number or {"rel": n}
    wipe    {ms, mask?}                      reveal staged changes (fade, or image mask)
    flash   {color, count}
    box     {box, show}                      show/hide a text box background
    roll    {src, ms, skipIf}                scroll a tall image bottom-to-top (staff roll)
    endscene                                 drop all images without staging a transition

  Screen, layered games (Little Busters). Same staging rule: nothing shows until `wipe`.
    init    {layer}                          remove everything on a layer (0 backgrounds, 1 sprites)
    img     {layer, slot, src | color, x, y, ax, ay, base?}
                                             put an image in a slot of a layer. Its point
                                             (ax, ay) goes to screen (x, y). `base` is
                                             {src, ax, ay}: a body image drawn first, at
                                             the same (x, y), with this image over it.
                                             alpha (0..1) makes it translucent. Without
                                             src and color the slot is emptied. Slots are
                                             drawn in ascending order
    shake   {style}                          shake the screen briefly
    video   {src}                            play a movie full screen, then continue
    voicestop                                stop the current voice clip

  Timing
    wait    {ms, skip}                       skip=true: a tap ends it early. No ms: wait for a tap

  Sound
    bgm     {id, ms, vol, loop?}             fade to vol over ms; id null or vol 0 = fade out and stop
    bgmloop {id, on}                         on: play intro once, then loop the marked section
    se      {ch, src, ms, vol, loop}         play on channel ch (voice: true = voice volume)
    sevol   {ch, ms, vol}                    fade channel (ch "*" = all); vol 0 stops it

  Text
    text    {box, id, keep, log, items}      items in order:
              {"t": "text"}                  "\\n" starts a new paragraph
              {"k": 1}                       wait for a tap (2 = end of page)
              {"v": file, "who": id}         voice clip for the text that follows
              {"do": op}                     run an op at this point
            keep 1: box stays until the next clear; 0: box hides; 2: text stays too
    textclear                                remove text kept with keep 2
    log     {text}                           add a line to the backlog only

  Flow
    set     {var, val}                       "$name" is saved with the game, "#name" is global.
                                             Numbered games: var is a number, or ["at", n, expr]
                                             for variable n + expr
    rand    {var, lo, hi}                    random integer, both ends included
    fill    {from, to, val}                  set a run of numbered variables
    setstr  {slot, text}                     text for $(slot,1) placeholders in later lines
    jz      {cond, to}                       jump to op index `to` when cond is false
    jnz     {cond, to}                       jump when cond is true
    jmp     {to}
    switch  {on, targets}                    jump to targets[on]; out of range = carry on
    choice  {options: [text], var, base}     stores the pick in var, counting from `base` (default 1)
            when: [expr per option]          an option whose expression is false is not offered;
                                             the stored number still counts every option
    goto    {scene, to?}                     scene id or expression; `to` = op index to start at
    call    {scene, to, args}                run another scene from op `to`, then come back.
                                             args go to manifest.vars.argBase, +1, ...
    ret                                      return from `call`
    gosub   {to} / return                    subroutine inside the same scene
    end                                      end of scene: return if called, otherwise the story
                                             is finished and the player goes back to the title
    ext     {name, w, text?}                 a command of the original engine that the player
                                             does not need (minigames, trophies); safe to skip

  Text, extra fields used by Little Busters
    who     speaker name shown above the line
    hold    the line stays on screen: the script carries on, or the next line joins it
    dyn     the text contains $(slot,1) or $v(expression) placeholders
    face    small portrait image shown with the line

Expressions are JSON values: numbers, strings, booleans, null, or
[operator, args...] with operators var (["var", "$x"] or ["var", 6001]), at
(["at", 6451, expr] = variable 6451 + expr), !, neg, ==, !=, <, >, <=, >=, &&, ||,
+, -, *, /, %, &, |.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

FORMAT_VERSION = 1


def write_bundle(out_path: Path, manifest: dict, scenes: dict[str, list], media: dict[str, Path],
                 progress=None) -> dict:
    """Write the bundle zip. Returns {"files": n, "bytes": size of the zip}."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")

    manifest = dict(manifest)
    manifest["format"] = FORMAT_VERSION
    manifest["scenes"] = sorted(scenes)
    manifest["stats"] = {
        "files": len(media) + len(scenes) + 1,
        "mediaBytes": sum(p.stat().st_size for p in media.values()),
    }

    def dumps(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    names = sorted(media)
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        zf.writestr(zipfile.ZipInfo("manifest.json"), dumps(manifest), zipfile.ZIP_DEFLATED, 9)
        for scene_id in sorted(scenes):
            zf.writestr(zipfile.ZipInfo(f"script/{scene_id}.json"),
                        dumps({"id": scene_id, "ops": scenes[scene_id]}), zipfile.ZIP_DEFLATED, 9)
        for i, name in enumerate(names):
            zf.write(media[name], name, zipfile.ZIP_STORED)
            if progress and (i % 200 == 0 or i == len(names) - 1):
                progress(i + 1, len(names))
    tmp.replace(out_path)
    return {"files": manifest["stats"]["files"], "bytes": out_path.stat().st_size}
