#!/usr/bin/env python3
"""vizr - a terminal music visualizer.

Particles on a braille grid, twenty-four ways of arranging them, and a
now-playing bar if playerctl is around. Audio comes from parec, or
sounddevice, or a fake signal when neither works.

Needs Python 3.8+, numpy, and a terminal that does true colour. Optional:
playerctl for the HUD, Pillow for album art.

? shows the keys, s opens settings. Config lives in ~/.config/vizr.
"""
from __future__ import annotations

import argparse
import colorsys
import io
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np

VERSION = '5.1.0'

# where to find the project and the person behind it
REPO = 'https://github.com/exotic-erox/vizr'
PYPI = 'https://pypi.org/project/vizr/'
GITHUB = 'https://github.com/exotic-erox'
DISCORD = 'https://discord.com/users/1515183303995560096'
AUTHOR = 'Erox'


# Constants

SR = 44100                            # capture sample rate
NF = 2048                             # FFT window
HANN = np.hanning(NF).astype(np.float32)

# braille dot bit values for a 2 (x) by 4 (y) sub-cell grid
BW = np.array([[1, 8], [2, 16], [4, 32], [64, 128]], dtype=np.int32)
BRAILLE = [chr(0x2800 + k) for k in range(256)]

# index is the mode id, so keep the order: the first ten are on the number
# row, presets point at names, and people memorise positions.
MODES = ['bars', 'mirror', 'radial', 'wave', 'galaxy', 'helix', 'sphere', 'flow',
         'aurora', 'terrain', 'lissajous', 'ripple', 'tunnel', 'spiral', 'scope',
         'warp', 'mandala', 'spectrogram', 'torus', 'rain', 'bloom', 'vortex',
         'ribbons', 'comets']
HUE_STEPS = 16                        # colour columns in the LUT
LEVELS = 16                           # brightness rows in the LUT

RESET = '\x1b[0m'
BOLD = '\x1b[1m'
MOUSE_RE = re.compile(r'\x1b\[<(\d+);(\d+);(\d+)([Mm])')
CSI_RE = re.compile(r'\x1b\[([0-9;?<]*)([A-Za-z~])')
SS3_RE = re.compile(r'\x1bO([A-Za-z])')
CSI_KEYS = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left',
            'H': 'home', 'F': 'end', 'Z': 'btab'}
TILDE_KEYS = {'1': 'home', '2': 'ins', '3': 'del', '4': 'end',
              '5': 'pgup', '6': 'pgdn', '7': 'home', '8': 'end'}


def fg(r, g, b):
    return '\x1b[38;2;%d;%d;%dm' % (r, g, b)


def bgc(r, g, b):
    return '\x1b[48;2;%d;%d;%dm' % (r, g, b)


# ui palette - kept independent of the particle accent
C_DIM = fg(132, 146, 166)             # >= 6:1 on the panel background
C_TXT = fg(208, 216, 226)
C_WHITE = '\x1b[1;38;2;240;244;247m'
C_OK = fg(120, 220, 140)
PANEL_BG = bgc(11, 13, 24)
SEL_BG = bgc(27, 31, 56)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def open_url(url):
    """xdg-open if it's there, webbrowser if not."""
    opener = shutil.which('xdg-open') or shutil.which('open')
    try:
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        import webbrowser
        return webbrowser.open(url)
    except (OSError, subprocess.SubprocessError):
        return False


def fmt_time(x):
    x = int(max(0, x))
    if x >= 3600:
        return '%d:%02d:%02d' % (x // 3600, x // 60 % 60, x % 60)
    return '%d:%02d' % (x // 60, x % 60)


# Terminal text helpers

def cw(ch):
    """Terminal cell width of one character."""
    if ch < ' ' or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def tw(text):
    return sum(cw(c) for c in text)


def trunc(text, w, ell='\u2026'):
    """Clip to w cells, add ellipsis if we had to cut."""
    if w <= 0:
        return ''
    if tw(text) <= w:
        return text
    lim = w - tw(ell)
    out, used = [], 0
    for ch in text:
        c = cw(ch)
        if used + c > lim:
            break
        out.append(ch)
        used += c
    return ''.join(out) + ell


def pad(text, w):
    text = trunc(text, w, '')
    return text + ' ' * (w - tw(text))


def fit(segs, w, base):
    """Render styled segments into exactly w cells (clip + pad)."""
    out, left = [], w
    for style, text in segs:
        if left <= 0:
            break
        t = trunc(text, left, '')
        out.append(RESET + base + style + t)
        left -= tw(t)
    if left > 0:
        out.append(RESET + base + ' ' * left)
    return ''.join(out) + RESET


# Colours and themes

ACCENTS: Dict[str, Tuple[int, int, int]] = {
    'cyan': (53, 228, 255), 'violet': (167, 139, 250), 'lime': (190, 255, 90),
    'red': (255, 90, 110), 'amber': (255, 176, 60), 'pink': (255, 105, 180),
    'teal': (45, 212, 191), 'orange': (255, 128, 40), 'blue': (96, 165, 250),
    'white': (232, 238, 242), 'green': (34, 197, 94), 'indigo': (99, 102, 241),
    'yellow': (255, 255, 11), 'acid': (127, 210, 67),
}
ACCENT_NAMES = tuple(ACCENTS)

BACKGROUND_RGB: Dict[str, Tuple[int, int, int]] = {
    'dark': (4, 7, 10), 'black': (0, 0, 0), 'navy': (4, 9, 22),
    'purple': (11, 5, 20), 'forest': (3, 12, 8), 'midnight': (15, 15, 35),
    'cyberpunk': (35, 20, 18), 'blood': (45, 0, 3),
}
BACKGROUNDS: Dict[str, str] = {k: bgc(*v) for k, v in BACKGROUND_RGB.items()}
BACKGROUNDS['transparent'] = ''

# name -> (accent, accent2, colour mode, background)
THEMES: Dict[str, Tuple[str, str, str, str]] = {
    'Classic': ('cyan', 'violet', 'energy', 'dark'),
    'Aurora': ('cyan', 'violet', 'gradient', 'dark'),
    'Ember': ('red', 'amber', 'height', 'black'),
    'Toxic': ('lime', 'teal', 'velocity', 'black'),
    'Mono': ('white', 'cyan', 'energy', 'black'),
    'Neon': ('pink', 'cyan', 'gradient', 'black'),
    'Sunset': ('orange', 'pink', 'height', 'purple'),
    'Ocean': ('blue', 'teal', 'height', 'navy'),
    'Borealis': ('lime', 'violet', 'height', 'forest'),
    'Prism': ('cyan', 'pink', 'rainbow', 'black'),
    'Pulse': ('indigo', 'green', 'gradient', 'midnight'),
    'Cyberpunk': ('yellow', 'acid', 'gradient', 'cyberpunk'),
}
COLOR_MODES = ('energy', 'gradient', 'velocity', 'height', 'rainbow', 'solid')

RGB = Tuple[float, float, float]


def lerp_bases(a, b):
    """HUE_STEPS colours blended from a to b."""
    out = []
    for h in range(HUE_STEPS):
        k = h / (HUE_STEPS - 1)
        out.append(tuple(a[i] + (b[i] - a[i]) * k for i in range(3)))
    return out


def rainbow_bases(offset=0.0):
    out = []
    for h in range(HUE_STEPS):
        r, g, b = colorsys.hsv_to_rgb((h / HUE_STEPS + offset) % 1.0, .82, 1.0)
        out.append((r * 255, g * 255, b * 255))
    return out


def rotate_hue(c, offset):
    h, s, v = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
    r, g, b = colorsys.hsv_to_rgb((h + offset) % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def build_lut(bases, brightness, glow):
    """Flat LUT of HUE_STEPS * LEVELS escape strings (index = hue * 16 + level)."""
    out = []
    for base in bases:
        for i in range(LEVELS):
            t = i / (LEVELS - 1)
            if t < .72:
                m = .5 + .5 * t / .72
                c = [v * m for v in base]
            else:
                m = (t - .72) / .28
                c = [v + (255 - v) * glow * m for v in base]
            c = [int(clamp(v * brightness, 0, 255)) for v in c]
            out.append(fg(*c))
    return out


# Settings
# The settings panel is built from this table, so keep the options together.

@dataclass
class Setting:
    key: str
    label: str
    tab: str
    kind: str                         # slider | int | choice | toggle | action
    default: Any = None
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.05
    choices: Tuple[str, ...] = ()
    fmt: str = '{:.2f}'
    desc: str = ''
    zero: str = ''
    log: bool = False
    url: str = ''


def slider(key, label, tab, default, lo, hi, step, fmt='{:.2f}', desc='', zero=''):
    return Setting(key, label, tab, 'slider', default, lo, hi, step, fmt=fmt, desc=desc, zero=zero)


def intslider(key, label, tab, default, lo, hi, step, fmt='{:,}', desc='', zero='', log=False):
    return Setting(key, label, tab, 'int', default, lo, hi, step, fmt=fmt, desc=desc, zero=zero, log=log)


def choice(key, label, tab, default, choices, desc=''):
    return Setting(key, label, tab, 'choice', default, choices=tuple(choices), desc=desc)


def toggle(key, label, tab, default, desc=''):
    return Setting(key, label, tab, 'toggle', default, desc=desc)


def action(key, label, tab, desc=''):
    return Setting(key, label, tab, 'action', None, desc=desc)


def link(key, label, tab, url, desc=''):
    """A settings row that's a URL, not a value."""
    return Setting(key, label, tab, 'link', None, desc=desc, url=url)


TABS = ('Display', 'Particles', 'Physics', 'Audio', 'HUD', 'Player', 'System',
        'Presets', 'Credits')
TABS_SHORT = ('Disp', 'Part', 'Phys', 'Aud', 'HUD', 'Play', 'Sys', 'Pre', 'Cred')

SETTINGS: List[Setting] = [
    # display
    choice('mode', 'Visual mode', 'Display', 'bars', MODES,
           'Visualization to draw. Keys 1-9 and 0 reach the first ten, '
           'm/M or left/right cycles all twenty.'),
    choice('theme', 'Theme', 'Display', 'Classic', tuple(THEMES),
           'Sets accent, second accent, colour mode and background together.'),
    choice('accent', 'Accent colour', 'Display', 'cyan', ACCENT_NAMES,
           'Primary particle colour, also used by the HUD.'),
    choice('accent2', 'Second accent', 'Display', 'violet', ACCENT_NAMES,
           'Second colour blended in by gradient / height / velocity modes.'),
    choice('color_mode', 'Colour mode', 'Display', 'energy', COLOR_MODES,
           'energy = loudness, gradient = left to right, velocity = speed, '
           'height = top to bottom, rainbow = full spectrum, solid = flat.'),
    toggle('art_colors', 'Album art colours', 'Display', True,
           'Tint particles with colours from the current cover art (needs Pillow). '
           'Turns energy mode into gradient.'),
    slider('hue_speed', 'Colour cycling', 'Display', 0.0, 0, 3, .1, zero='off',
           desc='Continuously rotate all colours. Wild with rainbow mode.'),
    slider('brightness', 'Brightness', 'Display', 1.0, .3, 1.6, .05,
           desc='Global brightness multiplier for all particles.'),
    slider('glow', 'Hot-spot glow', 'Display', .8, 0, 1, .05,
           desc='How strongly energetic particles blow out towards white.'),
    slider('trail', 'Motion trails', 'Display', 0.0, 0, .95, .05, zero='off',
           desc='Persistence of lit dots. Higher = longer tails (costs a bit of CPU).'),
    slider('flash', 'Beat flash', 'Display', .35, 0, 1, .05, zero='off',
           desc='Background pulses with the accent colour on every beat.'),
    slider('sparkle', 'Sparkle', 'Display', .15, 0, 1, .05, zero='off',
           desc='Random bright twinkles on fast-moving particles.'),
    slider('morph', 'Mode morph time', 'Display', .45, 0, 1.2, .05, fmt='{:.2f}s', zero='instant',
           desc='How long the cloud takes to glide into a new mode.'),
    choice('symmetry', 'Symmetry', 'Display', 'off', ('off', 'mirror-h', 'mirror-v', 'quad'),
           'Kaleidoscope mirroring. Key: Y.'),
    choice('background', 'Background', 'Display', 'dark', tuple(BACKGROUNDS),
           'Fill colour. "transparent" keeps your terminal theme.'),
    toggle('hud', 'Show HUD', 'Display', True, 'The two-line now-playing bar. Key: H.'),
    choice('hud_pos', 'HUD position', 'Display', 'bottom', ('bottom', 'top'),
           'Bottom or top of screen.'),

    # particles
    choice('count_mode', 'Count mode', 'Particles', 'auto', ('auto', 'manual'),
           'auto scales with terminal size * density; manual uses an exact count.'),
    slider('density', 'Density (auto)', 'Particles', 1.0, .1, 3.0, .05,
           desc='Particles per screen area when count mode is auto.'),
    intslider('count', 'Particle count (manual)', 'Particles', 24000, 1000, 150000, .08, log=True,
              desc='Exact count in manual mode. Hold shift for big steps.'),
    intslider('max_particles', 'Max particles', 'Particles', 60000, 5000, 250000, .08, log=True,
              desc='Hard ceiling for the auto count so huge terminals stay smooth.'),
    intslider('bands', 'Spectrum bands', 'Particles', 0, 0, 128, 4, fmt='{}', zero='auto',
              desc='Frequency columns the particles are grouped into.'),
    slider('bar_width', 'Bar fill', 'Particles', .78, .2, 1.0, .02,
           desc='Fraction of each column particles may occupy in bars/mirror.'),

    # physics
    slider('spring', 'Spring stiffness', 'Physics', .11, .02, .4, .01,
           desc='How hard particles are pulled toward their target shape.'),
    slider('damping', 'Damping', 'Physics', .8, .5, .97, .01,
           desc='Velocity retained per frame. Low = snappy, high = floaty.'),
    slider('jitter', 'Idle shimmer', 'Physics', .6, 0, 3, .1,
           desc='Amplitude of the little wobble that keeps shapes alive.'),
    slider('flow', 'Flow speed', 'Physics', 1.0, 0, 4, .1, zero='frozen',
           desc='Speed of rotation / drifting in the organic modes.'),
    slider('beat_force', 'Beat kick', 'Physics', 1.0, 0, 3, .1, zero='off',
           desc='Radial impulse on every detected beat.'),
    slider('beat_thresh', 'Beat sensitivity', 'Physics', 1.3, 1.05, 2.5, .05,
           desc='Bass must exceed its running avg by this factor to count as a beat.'),
    slider('beat_cooldown', 'Beat cooldown', 'Physics', .16, .05, .6, .01, fmt='{:.2f}s',
           desc='Minimum time between two detected beats.'),
    slider('flow_scale', 'Flow field zoom', 'Physics', 1.0, .4, 3.0, .05,
           desc='Size of the swirls in flow mode (higher = smaller eddies).'),
    slider('ripple_speed', 'Ripple speed', 'Physics', 1.0, .3, 2.5, .05,
           desc='How fast rings expand. Clicking spawns one too.'),
    choice('mouse_mode', 'Pointer effect', 'Physics', 'repel', ('repel', 'attract', 'vortex', 'off'),
           'What the mouse cursor does to nearby particles.'),
    slider('mouse_force', 'Pointer force', 'Physics', 1.0, 0, 3, .1, zero='off',
           desc='Strength of the pointer effect.'),
    slider('mouse_radius', 'Pointer radius', 'Physics', 1.0, .4, 3.0, .05,
           desc='Size of the influence area. Also: mouse wheel.'),
    slider('shock_force', 'Click shockwave', 'Physics', 1.0, 0, 3, .1, zero='off',
           desc='Burst on left click (push) / right click (pull).'),

    # audio
    slider('sens', 'Sensitivity', 'Audio', 1.0, .2, 5.0, .05,
           desc='Master gain on the spectrum. Also: +/- keys.'),
    slider('attack', 'Attack', 'Audio', .65, .1, 1.0, .05,
           desc='How fast levels rise when a band gets louder.'),
    slider('release', 'Release', 'Audio', .14, .02, .6, .01,
           desc='How fast levels fall when a band goes quiet.'),
    slider('gate', 'Noise gate', 'Audio', .0003, 0, .005, .0001, fmt='{:.4f}', zero='off',
           desc='Signals under this RMS are treated as silence.'),
    intslider('fmin', 'Lowest frequency', 'Audio', 40, 20, 200, 5, fmt='{} Hz',
              desc='Left edge of the analyzed spectrum.'),
    intslider('fmax', 'Highest frequency', 'Audio', 14000, 4000, 20000, 500, fmt='{} Hz',
              desc='Right edge of the analyzed spectrum.'),
    slider('tilt', 'Spectral tilt', 'Audio', .4, 0, 1, .05,
           desc='Boosts treble so it is as visible as bass (0 = flat).'),
    toggle('agc', 'Auto gain', 'Audio', True,
           'Normalize to the loudest recent peak. Off = absolute levels.'),
    slider('agc_decay', 'Auto-gain recovery', 'Audio', .9985, .99, .9999, .0005, fmt='{:.4f}',
           desc='How slowly the gain recovers after a loud peak.'),
    toggle('idle_anim', 'Idle animation', 'Audio', True,
           'Gently animate when nothing is playing instead of freezing.'),
    choice('source', 'Audio source', 'Audio', 'auto', ('auto', 'parec', 'sounddevice', 'demo'),
           'auto tries parec, then sounddevice, then the demo signal.'),
    action('act_restart_audio', 'Restart audio capture', 'Audio',
           'Re-open the capture device (do this after changing default output).'),

    # hud
    toggle('show_album', 'Show album', 'HUD', True, 'Append album name after title/artist.'),
    toggle('show_progress', 'Show progress bar', 'HUD', True, 'Seek bar with times. Click it to seek.'),
    toggle('show_source', 'Show player name', 'HUD', True, 'Tag showing which player info came from.'),
    toggle('marquee', 'Scroll long titles', 'HUD', True, 'Scroll instead of cutting long titles.'),
    toggle('show_meter', 'Show spectrum meter', 'HUD', True, 'Tiny live equalizer in the HUD.'),
    toggle('show_hints', 'Show key hints', 'HUD', True, 'Small "? help  s settings" hint in the HUD.'),

    # player
    action('act_play', 'Play / pause', 'Player', 'Toggle playback. Key: Space.'),
    action('act_prev', 'Previous track', 'Player', 'Skip back. Key: comma.'),
    action('act_next', 'Next track', 'Player', 'Skip forward. Key: period.'),
    action('act_seek_back', 'Seek backwards', 'Player', 'Jump back by the seek step. Key: [ (shift: 3x).'),
    action('act_seek_fwd', 'Seek forwards', 'Player', 'Jump forward. Key: ] (shift: 3x).'),
    action('act_shuffle', 'Toggle shuffle', 'Player', 'Shuffle on/off. Key: Z. Needs player support.'),
    action('act_loop', 'Cycle loop mode', 'Player', 'Off -> playlist -> track. Key: L.'),
    action('act_vol_up', 'Volume up', 'Player', 'Raise player volume. Key: V.'),
    action('act_vol_down', 'Volume down', 'Player', 'Lower player volume. Key: Shift+V.'),
    action('act_cycle_player', 'Cycle player now', 'Player',
           'Step through auto and each detected player (same as P).'),
    choice('player_mode', 'Player selection', 'Player', 'priority', ('priority', 'playing-first', 'manual'),
           'priority = Spotify beats browsers, playing-first = prefer a playing player, '
           'manual = locked via P.'),
    intslider('seek_step', 'Seek step', 'Player', 5, 1, 60, 1, fmt='{}s',
              desc='Seconds skipped by [ and ] (shift does 3x).'),
    intslider('volume_step', 'Volume step', 'Player', 5, 1, 25, 1, fmt='{}%',
              desc='Percent per volume keypress or wheel notch.'),
    slider('poll', 'Poll interval', 'Player', 1.0, .25, 5, .25, fmt='{:.2f}s',
           desc='How often player metadata is refreshed.'),
    toggle('side_panel', 'Info sidebar', 'Player', False,
           'Sidebar with cover art, track info, controls, volume and player list. Key: I.'),
    choice('side_pos', 'Sidebar side', 'Player', 'right', ('right', 'left'),
           'Which edge the sidebar sits on.'),
    intslider('side_width', 'Sidebar width', 'Player', 36, 26, 64, 2, fmt='{} cols',
              desc='Total width including the border.'),
    toggle('side_cover', 'Show cover art', 'Player', True,
           'Draw the album cover in the sidebar (needs Pillow; otherwise a placeholder).'),
    toggle('side_players', 'Show player list', 'Player', True,
           'List detected players in the sidebar; click to lock the HUD to one.'),

    # system
    intslider('fps', 'Target FPS', 'System', 50, 10, 144, 5, fmt='{}',
              desc='Frame rate cap. Lower it if your terminal struggles.'),
    toggle('adaptive', 'Adaptive quality', 'System', True,
           'Render fewer particles when frames take too long.'),
    slider('min_quality', 'Minimum quality', 'System', .35, .1, 1, .05,
           desc='Adaptive quality never goes below this fraction.'),
    toggle('mouse', 'Mouse support', 'System', True, 'Capture mouse for pointer physics and panel drags.'),
    toggle('autocycle', 'Auto-cycle modes', 'System', False, 'Rotate through modes automatically. Key: A.'),
    intslider('autocycle_s', 'Cycle interval', 'System', 20, 5, 120, 5, fmt='{}s',
              desc='Seconds per mode when auto-cycling.'),
    toggle('toasts', 'Show notifications', 'System', True, 'Pop-ups when a hotkey changes something.'),
    toggle('show_fps', 'Show performance', 'System', False, 'FPS, particle count and quality in the HUD. Key: F.'),
    toggle('autosave', 'Save on exit', 'System', True, 'Write the config on quit.'),
    action('act_save', 'Save settings now', 'System', 'Write current settings to disk. Key: W.'),
    action('act_reset', 'Reset all settings', 'System', 'Reset everything to defaults (press twice).'),

    # presets
    action('preset_Default', 'Preset: Default', 'Presets', 'Classic cyan look, balanced physics.'),
    action('preset_Minimal', 'Preset: Minimal', 'Presets', 'Light, monochrome, cheap to render.'),
    action('preset_Dense', 'Preset: Dense', 'Presets', 'Huge clouds with trails and strong glow.'),
    action('preset_Chill', 'Preset: Chill', 'Presets', 'Slow floaty galaxy in blue tones.'),
    action('preset_Neon', 'Preset: Neon', 'Presets', 'Hot pink / cyan gradient with hard beats.'),
    action('preset_Punchy', 'Preset: Punchy', 'Presets', 'Tight fast bars with violent kicks.'),
    action('preset_Kaleido', 'Preset: Kaleidoscope', 'Presets', 'Rainbow flow field mirrored four ways, long trails.'),
    action('preset_Synthwave', 'Preset: Synthwave', 'Presets', 'Sunset terrain ridgelines with pulsing background.'),
    action('preset_Cosmos', 'Preset: Cosmos', 'Presets', 'Dense 3D sphere with art colours and soft trails.'),
    action('preset_Hyperspace', 'Preset: Hyperspace', 'Presets', 'Tunnel and warp streaks with heavy bass kick.'),
    action('preset_Zen', 'Preset: Zen', 'Presets', 'Slow mandala petals, muted colour, no flashing.'),
    action('preset_Bloom', 'Preset: Bloom', 'Presets', 'Flower per band, soft gradient, slow morph.'),
    action('preset_Turbine', 'Preset: Turbine', 'Presets', 'Vortex shear with sparkle and hard kicks.'),
    action('save_1', 'Save to user slot 1', 'Presets', 'Store visual/physics/audio settings in slot 1.'),
    action('save_2', 'Save to user slot 2', 'Presets', 'Store visual/physics/audio settings in slot 2.'),
    action('save_3', 'Save to user slot 3', 'Presets', 'Store visual/physics/audio settings in slot 3.'),
    action('load_1', 'Load user slot 1', 'Presets', 'Recall slot 1.'),
    action('load_2', 'Load user slot 2', 'Presets', 'Recall slot 2.'),
    action('load_3', 'Load user slot 3', 'Presets', 'Recall slot 3.'),

    # not settings, but the panel is a good place for them
    link('cr_repo', 'Source code', 'Credits', REPO,
         'The repository - issues, releases and the full history.'),
    link('cr_pypi', 'PyPI package', 'Credits', PYPI,
         'Install or upgrade with:  pip install -U vizr'),
    link('cr_github', 'GitHub profile', 'Credits', GITHUB,
         'Everything else %s has built.' % AUTHOR),
    link('cr_discord', 'Discord', 'Credits', DISCORD,
         'Bug reports, feature ideas, or just say hi.'),
]

# tabs a preset is allowed to touch (system/fps/mouse etc left alone)
PRESET_TABS = ('Display', 'Particles', 'Physics', 'Audio')

PRESETS: Dict[str, Dict[str, Any]] = {
    'Default': {},
    'Minimal': dict(theme='Mono', trail=0.0, glow=.35, count_mode='manual', count=7000,
                    jitter=.3, beat_force=.5, mode='bars'),
    'Dense': dict(theme='Aurora', density=2.4, max_particles=140000, trail=.55, glow=.9,
                  mode='radial'),
    'Chill': dict(theme='Ocean', mode='galaxy', spring=.05, damping=.9, jitter=1.6,
                  release=.07, trail=.78, beat_force=.5, flow=.6),
    'Neon': dict(theme='Neon', glow=.95, trail=.5, beat_force=1.7, attack=.85,
                 brightness=1.2, mode='helix'),
    'Punchy': dict(theme='Ember', mode='bars', spring=.2, damping=.72, beat_force=2.2,
                   attack=.9, release=.22, beat_thresh=1.2),
    'Kaleido': dict(theme='Prism', mode='flow', symmetry='quad', trail=.78, hue_speed=.8,
                    density=2.0, glow=.9, flash=.2),
    'Synthwave': dict(theme='Sunset', mode='terrain', glow=.9, trail=.3, flash=.5,
                      brightness=1.15, damping=.84),
    'Cosmos': dict(theme='Aurora', mode='sphere', density=2.2, trail=.5, glow=.85,
                   art_colors=True, flash=.25),
    'Hyperspace': dict(theme='Prism', mode='tunnel', trail=.62, glow=1.0, flash=.4,
                       beat_force=1.8, flow=1.4, hue_speed=.5, density=1.6),
    'Zen': dict(theme='Mono', mode='mandala', trail=.35, glow=.5, flash=0.0,
                beat_force=.3, jitter=.3, damping=.88, flow=.4, brightness=.95),
    'Bloom': dict(theme='Borealis', mode='bloom', trail=.30, glow=.75, morph=.8,
                  spring=.07, damping=.86, density=1.4, sparkle=.2),
    'Turbine': dict(theme='Toxic', mode='vortex', trail=.55, glow=.9, sparkle=.45,
                    beat_force=2.0, flow=1.6, spring=.14, damping=.78, density=1.8),
}


def tab_settings(tab):
    return [s for s in SETTINGS if s.tab == tab]


# Config file handling

def default_config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'vizr', 'config.json')


class Config:
    """Values plus the bookkeeping: coercion, a dirty set so App knows what
    changed, and an atomic save. Nothing in here knows what a setting means -
    that all lives in the SETTINGS table."""

    def __init__(self, path):
        self.path = path
        self.defs = {s.key: s for s in SETTINGS}
        self.v = {s.key: s.default for s in SETTINGS if s.kind not in ('action', 'link')}
        self.dirty = set()
        self.slots = {}
        self.meta = {'seen_help': False, 'prefer': [], 'version': ''}

    def __getitem__(self, key):
        return self.v[key]

    def coerce(self, key, val):
        """Clamp it, or give up and use the default. Never raises."""
        s = self.defs[key]
        try:
            if s.kind == 'toggle':
                return bool(val)
            if s.kind == 'choice':
                return val if val in s.choices else s.default
            if s.kind == 'int':
                return int(clamp(round(float(val)), s.lo, s.hi))
            return float(clamp(float(val), s.lo, s.hi))
        except (TypeError, ValueError):
            return s.default

    def set(self, key, val):
        """True if it actually changed, so callers can skip the work."""
        val = self.coerce(key, val)
        if self.v.get(key) == val:
            return False
        self.v[key] = val
        self.dirty.add(key)
        return True

    def take_dirty(self):
        d, self.dirty = self.dirty, set()
        return d

    def reset(self, key):
        if key in self.v:
            self.set(key, self.defs[key].default)

    def reset_tabs(self, tabs):
        for s in SETTINGS:
            if s.tab in tabs and s.kind not in ('action', 'link'):
                self.set(s.key, s.default)

    def apply(self, values):
        for k, val in values.items():
            if k in self.v:
                self.set(k, val)

    def adjust(self, key, direction, mult=1.0):
        s = self.defs[key]
        v = self.v[key]
        if s.kind == 'choice':
            i = s.choices.index(v) if v in s.choices else 0
            self.set(key, s.choices[(i + direction) % len(s.choices)])
        elif s.kind == 'toggle':
            self.set(key, not v)
        elif s.kind in ('slider', 'int'):
            if s.log:
                nv = v * ((1 + s.step) ** (direction * mult))
                if s.kind == 'int' and int(round(nv)) == int(round(v)):
                    nv = v + direction
            else:
                nv = v + direction * s.step * mult
                nv = round(round(nv / s.step) * s.step, 6)
            self.set(key, nv)

    def frac(self, key):
        s = self.defs[key]
        v = float(self.v[key])
        if s.log:
            lo, hi = math.log(s.lo), math.log(max(s.hi, s.lo * 1.0001))
            return clamp((math.log(max(v, s.lo)) - lo) / (hi - lo), 0, 1)
        return clamp((v - s.lo) / ((s.hi - s.lo) or 1), 0, 1)

    def set_frac(self, key, f):
        s = self.defs[key]
        f = clamp(f, 0, 1)
        if s.log:
            v = math.exp(math.log(s.lo) + (math.log(s.hi) - math.log(s.lo)) * f)
            if s.kind == 'int':
                mag = 10 ** max(0, int(math.log10(max(v, 1))) - 2)
                v = round(v / mag) * mag
        else:
            v = s.lo + (s.hi - s.lo) * f
            v = round(round(v / s.step) * s.step, 6)
        self.set(key, v)

    @staticmethod
    def fmt_value(s, v):
        if s.kind == 'action':
            return 'run'
        if s.kind == 'link':
            return 'open'
        if s.kind == 'toggle':
            return 'ON' if v else 'off'
        if s.kind == 'choice':
            return str(v)
        if s.zero and v == 0:
            return s.zero
        return s.fmt.format(v)

    def display(self, key):
        return self.fmt_value(self.defs[key], self.v.get(key))

    def snapshot(self, tabs=PRESET_TABS):
        keys = [s.key for s in SETTINGS if s.tab in tabs and s.kind not in ('action', 'link')]
        return {k: self.v[k] for k in keys}

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return False
        try:
            with open(self.path, 'r', encoding='utf8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        for k, val in (data.get('values') or {}).items():
            if k in self.v:
                self.v[k] = self.coerce(k, val)
        self.slots = {str(k): v for k, v in (data.get('slots') or {}).items() if isinstance(v, dict)}
        meta = data.get('meta') or {}
        self.meta['seen_help'] = bool(meta.get('seen_help', False))
        self.meta['version'] = str(meta.get('version', ''))
        pref = meta.get('prefer') or []
        self.meta['prefer'] = [str(p).lower() for p in pref] if isinstance(pref, list) else []
        self.dirty.clear()
        return True

    def save(self):
        if not self.path:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf8') as fh:
                json.dump({'version': 2, 'values': self.v, 'slots': self.slots, 'meta': self.meta},
                          fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


# Audio capture

class Audio:
    """Provides NF mono samples. Prefers parec, falls back to sounddevice,
    then a built-in demo signal so it always has something to show."""

    def __init__(self, source='auto', device=None, monitor=None):
        self.buf = np.zeros(NF, np.float32)
        self.lk = threading.Lock()
        self.src = 'demo'
        self.label = 'demo signal'
        self.proc = None
        self.stream = None
        self.gen = 0
        self.device = device
        self.monitor = monitor or '@DEFAULT_MONITOR@'
        self.requested = source
        self.start(source)

    def start(self, source):
        """First backend that works wins: parec, then sounddevice, then demo."""
        self.stop()
        self.requested = source
        self.src, self.label = 'demo', 'demo signal'
        if source != 'demo':
            if source in ('auto', 'parec') and self._start_parec():
                return
            if source in ('auto', 'sounddevice') and self._start_sounddevice():
                return
            self.label = 'demo signal (no capture backend available)'

    def stop(self):
        self.gen += 1
        proc, self.proc = self.proc, None
        stream, self.stream = self.stream, None
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def close(self):
        self.stop()

    def _start_parec(self):
        if not shutil.which('parec'):
            return False
        try:
            proc = subprocess.Popen(
                ['parec', '-d', self.monitor, '--format=s16le', '--rate=%d' % SR,
                 '--channels=1', '--latency-msec=25'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            return False
        time.sleep(.25)
        if proc.poll() is not None:
            return False
        self.proc = proc
        self.src, self.label = 'live', 'live capture (parec)'
        threading.Thread(target=self._reader, args=(proc, self.gen), daemon=True).start()
        return True

    def _start_sounddevice(self):
        try:
            import sounddevice as sd
            dev = self.device
            if dev is not None and str(dev).isdigit():
                dev = int(dev)
            self.stream = sd.InputStream(samplerate=SR, channels=1, device=dev,
                                         blocksize=1024, callback=self._callback)
            self.stream.start()
        except Exception:
            self.stream = None
            return False
        self.src, self.label = 'live', 'live capture (sounddevice)'
        return True

    def _push(self, x):
        with self.lk:
            self.buf = np.concatenate((self.buf, x))[-NF:]

    def _reader(self, proc, gen):
        rest = b''
        while gen == self.gen:
            try:
                d = proc.stdout.read(2048)
            except (OSError, ValueError):
                break
            if not d:
                break
            rest += d
            n = len(rest) // 2 * 2
            self._push(np.frombuffer(rest[:n], np.int16).astype(np.float32) / 32768)
            rest = rest[n:]
        if gen == self.gen:
            self.src, self.label = 'demo', 'capture lost - showing demo signal'

    def _callback(self, indata, frames, tm, status):
        self._push(indata[:, 0].copy())

    def get(self):
        if self.src != 'demo':
            with self.lk:
                return self.buf.copy()
        # synthesized demo track so the app does something on first launch
        t = time.time() - np.arange(NF)[::-1] / SR
        ph = (t * 2) % 1
        kick = np.sin(2 * np.pi * (52 + 70 * np.exp(-ph * 25)) * ph) * np.exp(-ph * 7) * .8
        hat = np.random.randn(NF) * np.exp(-((t * 4 + .5) % 1) * 28) * .15
        lead = sum(np.sin(2 * np.pi * f * t) * (.5 + .5 * np.sin(t * (.6 + i * .37)))
                   for i, f in enumerate([220, 277.2, 329.6, 440, 880])) * .06
        bass = np.sin(2 * np.pi * (55 if int(t[-1] / 2) % 2 else 73.4) * t) * .25
        return (kick + hat + lead + bass).astype(np.float32)


# Spectrum analysis

class Spec:
    """Log-spaced FFT bands with attack/release smoothing and optional AGC."""

    FIXED_REF = 18.0

    def __init__(self):
        self.key = ()
        self.nb = 0
        self.sm = np.zeros(1)
        self.st = np.zeros(1, int)
        self.w = np.ones(1)
        self.pk = 6.0
        self.wp = .05

    def configure(self, nb, fmin, fmax, tilt):
        """Rebuild the band edges. Called every frame, so bail early when
        nothing changed."""
        key = (nb, fmin, fmax, tilt)
        if key == self.key:
            return
        self.key = key
        fmax = max(fmax, fmin * 2)
        f = np.fft.rfftfreq(NF, 1 / SR)
        e = np.geomspace(max(fmin, 20), fmax, nb + 1)
        ix = np.searchsorted(f, e).astype(int)
        # at 20 Hz the low bands land on the same bin; force them strictly
        # increasing so reduceat() never gets an empty slice
        ix[0] = max(ix[0], 1)
        for i in range(1, nb + 1):
            ix[i] = max(ix[i], ix[i - 1] + 1)
        ix = np.minimum(ix, len(f) - 1)
        self.st = ix[:-1].copy()
        self.w = (np.maximum(f[self.st], 60) / 250) ** tilt
        if nb != self.nb:
            self.sm = np.zeros(nb)
        self.nb = nb

    def feed(self, x, sens, attack, release, gate, agc, decay):
        m = np.abs(np.fft.rfft(x * HANN))
        v = np.sqrt(np.maximum.reduceat(m, self.st)) * self.w
        if agc:
            self.pk = max(self.pk * decay, float(v.max()), 5.0)
            ref = self.pk
        else:
            ref = self.FIXED_REF
        lvl = np.clip(v / ref * sens, 0, 1.15)
        if float(np.sqrt(np.mean(x * x))) < gate:
            lvl = lvl * 0
        up = lvl > self.sm
        self.sm = self.sm + (lvl - self.sm) * np.where(up, attack, release)
        return self.sm

    def wave(self, x):
        w = np.convolve(x[-1024:], np.ones(6) / 6, 'same')
        self.wp = max(self.wp * .995, float(np.abs(w).max()), .03)
        return w / self.wp


# Particle field

TAU = 6.283185307179586


class Field:
    """Particles. step() picks where they should be, render() turns that into
    braille cells. They talk through self.e - step writes the brightness,
    render reads it. Saves passing a big tuple around every frame."""

    ATTRS = ('x', 'y', 'vx', 'vy', 'pb', 'u', 'rx', 'xi', 'sg', 'slot', 'arm',
             'rn', 'ph', 'e')
    LANES = 16
    RINGS = 8

    def __init__(self, cols, rows, count, nb):
        self.C, self.R2 = cols, rows
        self.PW, self.PH = cols * 2, rows * 4
        self.nb = nb
        self.rot = 0.0
        self.pg = np.zeros((self.PH, self.PW), np.float32)
        self.hist = np.zeros((self.LANES, nb))
        self.hist_t = 0.0
        self.ring_t = np.full(self.RINGS, -1e9)
        self.ring_s = np.zeros(self.RINGS)
        self.ring_c = np.zeros((self.RINGS, 2))
        self.ring_i = 0
        self.last_ring = 0.0
        self.tun = 0.0                    # tunnel depth offset
        self.spec = None                  # spectrogram history, lazily sized
        self.spec_t = 0.0
        self.rhx = self.rhy = self.rL = None   # raw targets, this frame
        self.shx = self.shy = self.sL = None   # frozen targets, morph source
        count = max(256, int(count))
        for k, arr in self._make(count, 0).items():
            setattr(self, k, arr)
        self.N = count
        self.active = count

    def _make(self, k, start):
        nb = self.nb
        return {
            'x': np.random.rand(k) * self.PW,
            'y': np.random.rand(k) * self.PH,
            'vx': np.zeros(k), 'vy': np.zeros(k),
            'pb': (np.arange(start, start + k) % nb).astype(np.int64),
            'u': np.random.rand(k), 'rx': np.random.rand(k), 'xi': np.random.rand(k),
            'sg': np.random.choice([-1., 1.], k),
            'slot': np.random.randint(0, 2 * nb, k),
            'arm': np.random.randint(0, 3, k),
            'rn': np.sqrt(np.random.rand(k)),
            'ph': np.random.rand(k),
            'e': np.zeros(k),
        }

    def resize(self, n):
        n = max(256, int(n))
        if n < self.N:
            for a in self.ATTRS:
                setattr(self, a, getattr(self, a)[:n].copy())
        elif n > self.N:
            new = self._make(n - self.N, self.N)
            for a in self.ATTRS:
                setattr(self, a, np.concatenate((getattr(self, a), new[a])))
        self.N = n
        self.active = min(self.active, n)

    def set_quality(self, q):
        self.active = int(max(256, min(self.N, self.N * q)))

    def spawn_ring(self, t, strength, x, y):
        i = self.ring_i % self.RINGS
        self.ring_i += 1
        self.ring_t[i], self.ring_s[i] = t, strength
        self.ring_c[i] = (x, y)
        self.last_ring = t

    def snap(self):
        """Remember where everything was, so a mode change can glide instead
        of snapping. Does nothing on the first frame, or after flow (which
        has no targets to freeze)."""
        if self.rhx is None or self.rhy is None or self.rL is None:
            self.shx = self.shy = self.sL = None
            return
        self.shx = np.array(self.rhx, dtype=float, copy=True)
        self.shy = np.array(self.rhy, dtype=float, copy=True)
        self.sL = np.array(self.rL, dtype=float, copy=True)

    def step(self, mode, lv, bass, beat, mp, shocks, dt, t, wv, P, morph=1.0):
        """One tick. The mode branches only set hx/hy/L; the shared tail does
        the kicks, the pointer and the spring."""
        PW, PH, nb = self.PW, self.PH, self.nb
        a = self.active
        x, y, vx, vy = self.x[:a], self.y[:a], self.vx[:a], self.vy[:a]
        pb, u, rx, xi = self.pb[:a], self.u[:a], self.rx[:a], self.xi[:a]
        sg, slot, arm, rn, ph = self.sg[:a], self.slot[:a], self.arm[:a], self.rn[:a], self.ph[:a]
        f = min(dt * 60, 3)
        flow = P['flow']
        cx, cy = PW / 2, PH / 2
        free = False
        tvx = tvy = None
        hx = hy = L = None
        mn = min(PW, PH)

        # terrain mode uses a short history of the spectrum
        if t - self.hist_t > .07:
            self.hist[1:] = self.hist[:-1]
            self.hist[0] = lv
            self.hist_t = t

        if mode == 0:                                    # bars
            L = lv[pb]
            hx = (pb + .5) / nb * PW + (rx - .5) * PW / nb * P['bar_width']
            hy = PH - 1 - u * (L * .92 + .02) * (PH - 4)
            cy = PH
        elif mode == 1:                                  # mirror
            c = (nb - 1) / 2 or 1
            b = np.clip((np.abs(pb - c) / c * (nb - 1)).astype(int), 0, nb - 1)
            L = lv[b]
            hx = (pb + .5) / nb * PW + (rx - .5) * PW / nb * P['bar_width']
            hy = PH / 2 + sg * u * (L * .95 + .02) * (PH / 2 - 3)
        elif mode == 2:                                  # radial
            lm = np.concatenate((lv, lv[::-1]))
            L = lm[slot]
            th = (slot + rx) / (2 * nb) * TAU + t * .08 * flow
            r = mn * .17 * (1 + .25 * bass) + u * (L * .9 + .03) * mn * .33
            hx = cx + np.cos(th) * r
            hy = cy + np.sin(th) * r
        elif mode == 3:                                  # wave
            L = lv[np.clip((xi * nb).astype(int), 0, nb - 1)]
            w = np.interp(xi, np.linspace(0, 1, len(wv)), wv)
            hx = xi * PW
            hy = cy + w * PH * .36 + (u - .5) * (3 + L * PH * .22)
            L = np.clip(.3 + np.abs(w) * .7 + L * .3, 0, 1.1)
        elif mode == 4:                                  # galaxy
            self.rot += dt * (.25 + bass * 1.6) * flow
            L = lv[np.clip((rn * nb).astype(int), 0, nb - 1)]
            th = arm * 2.0944 + rn * (3 + bass * 1.5) + self.rot + (rx - .5) * .55 * (1 - rn * .5)
            r = rn * mn * .46 * (1 + .22 * bass) + L * PH * .07
            hx = cx + np.cos(th) * r
            hy = cy + np.sin(th) * r
            L = np.clip(L * .8 + (1 - rn) * .35, 0, 1.1)
        elif mode == 5:                                  # helix
            self.rot += dt * (.9 + bass * 2.5) * flow
            Lb = lv[np.clip((xi * nb).astype(int), 0, nb - 1)]
            amp = PH * .30 * (.35 + Lb * .9)
            pha = xi * TAU * 2.2 - self.rot
            ps = pha + np.where(arm == 1, 3.14159, 0.0)
            strand = np.sin(ps) * amp
            rung = np.sin(pha) * amp * (u * 2 - 1)
            hx = xi * PW
            hy = cy + np.where(arm == 2, rung, strand) + (rx - .5) * 2.2
            depth = np.where(arm == 2, .3, (np.cos(ps) + 1) / 2)
            L = np.clip(.2 + Lb * .5 + depth * .45, 0, 1.1)
        elif mode == 6:                                  # 3D audio sphere
            self.rot += dt * (.3 + bass * 1.1) * flow
            lat = np.arccos(1 - 2 * u)
            lon = rx * TAU + self.rot
            bi = np.clip((np.abs(lat - 1.5708) / 1.5708 * (nb - 1)).astype(int), 0, nb - 1)
            Lb = lv[bi]
            R0 = mn * .36 * (1 + .12 * bass)
            r = R0 * (1 + Lb * .5)
            X = r * np.sin(lat) * np.cos(lon)
            Z = r * np.sin(lat) * np.sin(lon)
            Y = r * np.cos(lat)
            if mp[0] > -1e3:
                tilt = .32 + (mp[1] / PH - .5) * 1.1
            else:
                tilt = .32 + .18 * math.sin(t * .27)
            ct, st = math.cos(tilt), math.sin(tilt)
            Y2 = Y * ct - Z * st
            Z2 = Y * st + Z * ct
            persp = 3.4 * R0 / (3.4 * R0 - Z2)
            hx = cx + X * persp
            hy = cy + Y2 * persp
            depth = (Z2 / np.maximum(r, 1) + 1) / 2
            L = np.clip(.12 + depth * .55 + Lb * .45, 0, 1.1)
        elif mode == 7:                                  # fluid flow field
            free = True
            k = TAU / (mn * .7) * P['flow_scale']
            fa = k * x + t * .6 * flow
            fb = 1.1 * k * y - t * .4 * flow
            fc = .5 * k * x - .7 * k * y + t * .3 * flow + 1.3
            Lx = lv[np.clip((x / PW * nb).astype(int), 0, nb - 1)]
            speed = (1.1 + bass * 3.5) * (.5 + Lx * 1.8) * flow * 1.6
            tvx = (np.sin(fa) * np.cos(fb) * 1.1 - .42 * np.cos(fc)) * speed
            tvy = -(np.cos(fa) * np.sin(fb) + .3 * np.cos(fc)) * speed
            L = np.clip(.22 + Lx * .65 + np.hypot(tvx, tvy) * .06, 0, 1.1)
            # respawn a few particles so the field never goes empty
            rs = np.random.rand(a) < .003
            n = int(rs.sum())
            if n:
                x[rs] = np.random.rand(n) * PW
                y[rs] = np.random.rand(n) * PH
                vx[rs], vy[rs] = tvx[rs], tvy[rs]
        elif mode == 8:                                  # aurora curtains
            nr = max(8, PW // 3)
            gx = (np.floor(xi * nr) + .5) / nr
            Lb = lv[np.clip((gx * nb).astype(int), 0, nb - 1)]
            wob = (np.sin(gx * TAU * 1.3 + t * .5 * flow + arm * 2.1) * .10
                   + np.sin(gx * TAU * 2.9 - t * .8 * flow + arm) * .05)
            yc = PH * (.36 + .16 * arm + wob)
            hgt = PH * (.10 + Lb * .38 + .05 * bass)
            hx = gx * PW + (rx - .5) * PW / nr * .8
            hy = yc - (u ** 1.7) * hgt
            L = np.clip((1 - u) * (.35 + Lb * .8) + .12, 0, 1.1)
        elif mode == 9:                                  # synthwave terrain
            lane = np.minimum((ph * self.LANES).astype(int), self.LANES - 1)
            d = lane / (self.LANES - 1)
            fi = np.abs(xi - .5) * 2 * (nb - 1)
            i0 = np.minimum(fi.astype(int), nb - 1)
            i1 = np.minimum(i0 + 1, nb - 1)
            w = fi - i0
            h = self.hist[lane, i0] * (1 - w) + self.hist[lane, i1] * w
            hx = cx + (xi - .5) * PW * (1 - .5 * d) * .96
            base = PH * .93 - d ** .85 * PH * .60
            hy = base - h * PH * .34 * (1 - .55 * d) + (rx - .5) * .6
            edge = np.clip(1 - np.abs(xi - .5) * 2, 0, 1) ** .35
            L = np.clip((.16 + (1 - d) * .4 + h * .55) * (.4 + .6 * edge), 0, 1.1)
        elif mode == 10:                                 # harmonograph
            th = ph * TAU * 2
            ta = t * .5 * flow
            ra = 3 + 2 * math.sin(ta * .31)
            rb = 2 + math.cos(ta * .23)
            Lb = lv[np.clip((ph * nb).astype(int), 0, nb - 1)]
            X = np.sin(ra * th + ta * .7) + .4 * np.sin(2 * ra * th + ta * 1.3)
            Y = np.sin(rb * th) + .4 * np.cos(3 * rb * th - ta * .9)
            amp = (1 + Lb * .35 + bass * .15) / 1.4
            hx = cx + X * PW * .30 * amp + (rx - .5) * (1.2 + Lb * 4)
            hy = cy + Y * PH * .36 * amp + (u - .5) * (1.2 + Lb * 4)
            L = np.clip(.3 + Lb * .6 + .2 * np.sin(th * 3 + ta), 0, 1.1)
        elif mode == 11:                                 # beat ripples
            if beat or t - self.last_ring > 1.6:
                self.spawn_ring(t, bass, cx, cy)
            for sx, sy, _kind in shocks:
                self.spawn_ring(t, .8, sx, sy)
            ring = slot % self.RINGS
            age = t - self.ring_t[ring]
            life = 1.3 / max(P['ripple_speed'], .1)
            alive = (age >= 0) & (age < life)
            ang = rx * TAU
            rs_ = self.ring_s[ring]
            r_alive = age * mn * .5 * P['ripple_speed'] + (u - .5) * (2 + rs_ * 4)
            Lc = lv[np.clip((rx * nb).astype(int), 0, nb - 1)]
            r_idle = mn * .14 * (1 + .25 * bass) + Lc * mn * .1 * u
            r = np.where(alive, r_alive, r_idle)
            hx = np.where(alive, self.ring_c[ring, 0], cx) + np.cos(ang) * r
            hy = np.where(alive, self.ring_c[ring, 1], cy) + np.sin(ang) * r
            fade = np.clip(1 - age / life, 0, 1)
            L = np.where(alive, np.clip(fade * (.5 + rs_ * 1.2) + .15, 0, 1.1),
                         np.clip(.2 + Lc * .6, 0, 1.1))
        elif mode == 12:                                 # tunnel
            # depth from xi, so the spring drags the far ones in by itself
            self.tun = (self.tun + dt * (.35 + bass * 1.4) * flow) % 1.0
            z = (xi + self.tun) % 1.0                    # 0 far .. 1 near
            th = rx * TAU + z * 1.6 + t * .1 * flow
            rad = mn * .04 + z * z * mn * .62
            wob = 1 + np.sin(pb / nb * TAU * 2 + t * .7) * .06 * bass
            hx = cx + np.cos(th) * rad * wob
            hy = cy + np.sin(th) * rad * wob * .72
            L = np.clip(.12 + (1 - z) * .22 + lv[pb] * .8 * z + z * .25, 0, 1.1)
        elif mode == 13:                                 # spiral
            # radius grows exponentially with ph - that's what makes it a
            # spiral instead of galaxy's scatter
            self.rot += dt * (.4 + bass * 1.2) * flow
            a0 = ph * 3.4                                # turns along the arm
            th = arm * TAU / 3 + a0 + self.rot
            rad = mn * .05 * np.exp(a0 * .62) + (u - .5) * (2 + lv[pb] * 6)
            rad = np.minimum(rad, mn * .55) * (1 + .1 * bass)
            hx = cx + np.cos(th) * rad
            hy = cy + np.sin(th) * rad * .86
            L = np.clip(.14 + lv[pb] * .7 + (1 - np.minimum(rad / (mn * .55), 1)) * .4, 0, 1.1)
        elif mode == 14:                                 # vector scope
            # two points a third of a buffer apart, plotted against each
            # other - a real X/Y scope, not a waveform
            n = len(wv)
            i0 = np.clip((xi * (n - 1)).astype(int), 0, n - 1)
            i1 = (i0 + n // 3) % n
            sx = wv[i0] * .5
            sy = wv[i1] * .5
            spin = t * .12 * flow
            cs, sn = math.cos(spin), math.sin(spin)
            hx = cx + (sx * cs - sy * sn) * PW * .86 + (u - .5) * 2
            hy = cy + (sx * sn + sy * cs) * PH * .86 + (rx - .5) * 2
            L = np.clip(.25 + np.hypot(sx, sy) * .9 + lv[pb] * .3, 0, 1.1)
        elif mode == 15:                                 # warp
            self.rot += dt * flow
            th = rx * TAU
            z = (xi + t * (.35 + bass * 2.2) * flow * .35) % 1.0
            rad = z * z * mn * .8
            hx = cx + np.cos(th + self.rot * .2) * rad
            hy = cy + np.sin(th + self.rot * .2) * rad * .9
            hx -= np.cos(th) * z * 6                     # smear the streak inward
            hy -= np.sin(th) * z * 6
            L = np.clip(.1 + z * .5 + lv[pb] * .6 * (1 - z) + bass * .2, 0, 1.1)
        elif mode == 16:                                 # mandala
            # r(θ) = |cos(kθ)| - the petals come free, and the symmetry is
            # built in so it doesn't fight the Y key
            pet = max(3, nb // 3)
            th = rx * TAU
            rk = np.abs(np.cos(pet * th / 2))
            Lb = lv[np.clip((u * nb).astype(int), 0, nb - 1)]
            rad = mn * (.08 + rk * .40) * (1 + .22 * bass) * (.45 + u * .75) + Lb * mn * .1
            hx = cx + np.cos(th) * rad
            hy = cy + np.sin(th) * rad
            L = np.clip(.18 + Lb * .7 + rk * .35, 0, 1.1)
        elif mode == 17:                                 # scrolling spectrogram
            # the grid doesn't move - the history under it scrolls, every
            # 50 ms
            cols = max(16, PW // 2)
            if self.spec is None or self.spec.shape != (cols, nb):
                self.spec = np.zeros((cols, nb))
                self.spec_t = 0.0
            if t - self.spec_t > .05:
                self.spec[1:] = self.spec[:-1]
                self.spec[0] = lv
                self.spec_t = t
            ci = np.clip((xi * (cols - 1)).astype(int), 0, cols - 1)
            v = self.spec[cols - 1 - ci, pb]
            hx = xi * PW + (rx - .5) * PW / cols * .8
            hy = PH - (pb + .5) / nb * PH + (u - .5) * PH / nb * .8
            L = np.clip(v * 1.1 + .02, 0, 1.1)
        elif mode == 18:                                 # torus
            self.rot += dt * (.3 + bass * 1.0) * flow
            Rr = mn * .28
            rr = mn * .10 * (1 + .35 * bass)
            ph2 = rx * TAU + self.rot
            th = u * TAU
            tb = np.clip((u * nb).astype(int), 0, nb - 1)
            rr2 = rr * (1 + lv[tb] * .8)
            X = (Rr + rr2 * np.cos(th)) * np.cos(ph2)
            Z = (Rr + rr2 * np.cos(th)) * np.sin(ph2)
            Y = rr2 * np.sin(th)
            if mp[0] > -1e3:
                tilt = .55 + (mp[1] / PH - .5) * .8
            else:
                tilt = .55 + .15 * math.sin(t * .21)
            ct, st = math.cos(tilt), math.sin(tilt)
            Y2 = Y * ct - Z * st
            Z2 = Y * st + Z * ct
            persp = 3.0 * mn / (3.0 * mn - Z2)
            hx = cx + X * persp
            hy = cy + Y2 * persp
            L = np.clip(.15 + lv[tb] * .65 + (np.cos(th) + 1) * .18 + (Z2 / mn + .5) * .1, 0, 1.1)
        elif mode == 19:                                 # rain
            # a band per column, each drop falling at its own band's speed
            Lb = lv[pb]
            fall = (ph + t * (.12 + Lb * .8) * flow) % 1.0
            hx = (pb + .5) / nb * PW + (rx - .5) * PW / nb * .85
            hy = fall * PH
            L = np.clip(.06 + Lb * .85 + fall * .12, 0, 1.1)
        elif mode == 20:                                 # bloom
            # a flower per band, petals opening with that band's level
            Lb = lv[pb]
            open_ = .25 + Lb * 1.15
            th = rx * TAU
            petal = np.abs(np.cos((3 + pb % 4) * th / 2)) ** 1.6
            rad = mn * (.04 + petal * .30 * open_) * (1 + .12 * bass)
            cxb = (pb + .5) / nb * PW
            cyb = PH * (.5 + .32 * np.sin(pb * 2.399 + t * .2 * flow))
            spread = .45 + u * .75
            hx = cxb + np.cos(th + arm * 2.1) * rad * spread
            hy = cyb + np.sin(th + arm * 2.1) * rad * spread
            L = np.clip(.1 + Lb * .85 + petal * .25, 0, 1.1)
        elif mode == 21:                                 # vortex
            # fast in the middle, slow at the rim - the shear is the effect
            rad = mn * (.05 + rn * .5)
            spin = 2.6 / (rad / mn + .2)
            th = rx * TAU + t * spin * .3 * flow
            rad = rad * (1 + .18 * bass + lv[pb] * .25)
            hx = cx + np.cos(th) * rad
            hy = cy + np.sin(th) * rad * .92
            L = np.clip(.1 + lv[pb] * .75 + (1 - rn) * .45, 0, 1.1)
        elif mode == 22:                                 # ribbons
            # three sine ribbons, thickness driven by the band underneath
            seg = arm % 3
            Lb = lv[np.clip((ph * nb).astype(int), 0, nb - 1)]
            base = PH * (.28 + .21 * seg)
            wav = np.sin(ph * TAU * (1.3 + seg * .35) + t * (.55 + seg * .22) * flow)
            hx = xi * PW
            hy = base + wav * PH * (.09 + Lb * .22) + (u - .5) * (2 + Lb * 7)
            L = np.clip(.16 + Lb * .8 + (np.cos(ph * TAU) + 1) * .12, 0, 1.1)
        else:                                            # comets
            # u**3 keeps only a few heads lit, like real comet nuclei
            self.rot += dt * (.18 + bass * .75) * flow
            th = ph * TAU + self.rot * (.4 + rn * .8)
            rad = mn * (.1 + rn * .36) * (1 + .25 * bass)
            hx = cx + np.cos(th) * rad * 1.18
            hy = cy + np.sin(th) * rad * .78
            L = np.clip(.06 + lv[pb] * .5 + u ** 3, 0, 1.1)

        # flow has no targets, only velocities - fall back to where we are so
        # the morph maths and snap() both have something to work with
        if hx is None or hy is None:
            hx, hy = x.copy(), y.copy()
        if L is None:
            L = self.e[:a]

        # untweened targets, for the next mode change to morph from
        self.rhx, self.rhy, self.rL = hx, hy, L
        if (morph < 1 and self.shx is not None and self.shy is not None
                and self.sL is not None and len(self.shx) == a):
            m = morph * morph * (3 - 2 * morph)          # smoothstep
            hx = self.shx + (np.asarray(hx, float) - self.shx) * m
            hy = self.shy + (np.asarray(hy, float) - self.shy) * m
            L = self.sL + (np.asarray(L, float) - self.sL) * m

        self.e[:a] = L

        # beat kick - radial impulse away from center
        if beat and P['beat_force'] > 0:
            dx, dy = x - cx, y - cy
            n = np.hypot(dx, dy) + 1
            g = (2.2 + bass * 5) * (.5 + u) * P['beat_force']
            vx += dx / n * g
            vy += dy / n * g

        # pointer
        R = max(8, mn * .15) * P['mouse_radius']
        mm = P['mouse_mode']
        if mm != 'off' and P['mouse_force'] > 0 and mp[0] > -1e3:
            dx, dy = x - mp[0], y - mp[1]
            d2 = dx * dx + dy * dy
            d = np.sqrt(d2) + 1e-6
            g = np.where(d2 < R * R, (1 - d / R) * 3.8 * P['mouse_force'], 0)
            if mm == 'repel':
                vx += dx / d * g
                vy += dy / d * g
            elif mm == 'attract':
                vx -= dx / d * g * .6
                vy -= dy / d * g * .6
            else:                                        # vortex
                vx += (-dy / d * .9 - dx / d * .25) * g
                vy += (dx / d * .9 - dy / d * .25) * g

        # click shockwaves
        if P['shock_force'] > 0:
            for sx, sy, kind in shocks:
                dx, dy = x - sx, y - sy
                d = np.hypot(dx, dy) + 1e-6
                Rr = R * 2.8
                g = np.where(d < Rr, (1 - d / Rr) * (16 if kind else -12) * P['shock_force'], 0)
                vx += dx / d * g
                vy += dy / d * g

        # integrate
        if free:
            kf = 1 - .88 ** f
            vx += (tvx - vx) * kf
            vy += (tvy - vy) * kf
            x[:] = (x + vx * f) % PW
            y[:] = (y + vy * f) % PH
        else:
            jit = P['jitter']
            hx = hx + np.sin(t * 1.3 + rx * 40) * jit
            hy = hy + np.cos(t * 1.1 + u * 40) * jit
            k = P['spring'] * f
            vx += (hx - x) * k
            vy += (hy - y) * k
            dmp = P['damping'] ** f
            vx *= dmp
            vy *= dmp
            np.clip(x + vx * f, 0, PW - .01, out=x)
            np.clip(y + vy * f, 0, PH - .01, out=y)

    def render(self, lut, bg, color_mode, trail, symmetry='off', sparkle=0.0, t=0.0):
        PW, PH, C, R2 = self.PW, self.PH, self.C, self.R2
        a = self.active
        ix = self.x[:a].astype(np.int32)
        iy = self.y[:a].astype(np.int32)
        sym_h = symmetry in ('mirror-h', 'quad')
        sym_v = symmetry in ('mirror-v', 'quad')

        if trail > .01:
            self.pg *= trail
            self.pg[iy, ix] = 1.0
            grid = self.pg > .22
        else:
            grid = np.zeros((PH, PW), bool)
            grid[iy, ix] = True
        if sym_h:
            grid = grid | grid[:, ::-1]
        if sym_v:
            grid = grid | grid[::-1, :]
        code = (grid.reshape(R2, 4, C, 2) * BW[None, :, None, :]).sum(axis=(1, 3))

        ci = (iy >> 2) * C + (ix >> 1)
        sz = R2 * C
        cnt = np.maximum(np.bincount(ci, minlength=sz), 1)
        se = (np.bincount(ci, weights=self.e[:a], minlength=sz) / cnt).reshape(R2, C)
        sp = np.hypot(self.vx[:a], self.vy[:a])
        ss = (np.bincount(ci, weights=sp, minlength=sz) / cnt).reshape(R2, C)

        def fold(arr):
            if sym_h:
                arr = np.maximum(arr, arr[:, ::-1])
            if sym_v:
                arr = np.maximum(arr, arr[::-1, :])
            return arr

        se, ss = fold(se), fold(ss)
        if color_mode == 'solid':
            lvl = np.full((R2, C), 11, np.int32)
        else:
            lvl = np.clip(((.3 + se * .55 + np.minimum(ss * .1, .35)) * 15).astype(int), 0, 15)

        # sparkle only lands on cells that already have particles, so it
        # twinkles instead of turning into noise
        if sparkle > .01:
            hot = ss > np.percentile(ss, 88) if ss.size else ss > 0
            tw = np.random.rand(R2, C) < sparkle * .09 * (.5 + .5 * np.sin(t * 7.3))
            lvl = np.where(hot & tw, 15, lvl)

        top = HUE_STEPS - 1
        cols = np.arange(C)
        rws = np.arange(R2)
        if sym_h:
            cols = np.minimum(cols, C - 1 - cols) * 2
        if sym_v:
            rws = np.minimum(rws, R2 - 1 - rws) * 2
        if color_mode in ('gradient', 'rainbow'):
            hz = np.broadcast_to((cols / max(C - 1, 1) * top).astype(int)[None, :], (R2, C))
        elif color_mode == 'height':
            hz = np.broadcast_to((rws / max(R2 - 1, 1) * top).astype(int)[:, None], (R2, C))
        elif color_mode == 'velocity':
            hz = np.clip((ss * .35 * HUE_STEPS).astype(int), 0, top)
        else:
            hz = np.zeros((R2, C), np.int32)
        idx = hz * LEVELS + lvl

        rows = []
        for cr, ir in zip(code.tolist(), idx.tolist()):
            out, last = [], -1
            for k, i in zip(cr, ir):
                if k:
                    if i != last:
                        out.append(lut[i])
                        last = i
                    out.append(BRAILLE[k])
                else:
                    out.append(' ')
            rows.append(bg + ''.join(out))
        return rows


# Media player integration (MPRIS)

class Track:
    """Snapshot of one player's state. Swapped atomically by the poll thread."""
    __slots__ = ('name', 'inst', 'artist', 'title', 'album', 'status', 'pos', 'ln', 'stamp',
                 'art', 'vol', 'loop', 'shuffle')

    def __init__(self, name='', inst='', artist='', title='', album='',
                 status='', pos=0.0, ln=0.0, stamp=0.0, art='', vol=-1.0, loop='',
                 shuffle=False):
        self.art, self.vol, self.loop, self.shuffle = art, vol, loop, shuffle
        self.name, self.inst = name, inst
        self.artist, self.title, self.album = artist, title, album
        self.status, self.pos, self.ln, self.stamp = status, pos, ln, stamp

    @property
    def playing(self):
        return self.status == 'Playing'

    @property
    def has_info(self):
        return bool(self.title or self.artist)

    def position(self):
        p = self.pos + (time.time() - self.stamp if self.playing else 0)
        return min(p, self.ln) if self.ln > 0 else p

    def copy(self, **kw):
        t = Track(self.name, self.inst, self.artist, self.title, self.album,
                  self.status, self.pos, self.ln, self.stamp, self.art, self.vol,
                  self.loop, self.shuffle)
        for k, val in kw.items():
            setattr(t, k, val)
        return t


class Media:
    """Polls MPRIS players via playerctl and picks one to display.

    Selection modes:
      priority       Spotify etc. rank above browsers (default). A YouTube
                     tab can't steal the HUD from Spotify.
      playing-first  prefer whichever player is currently playing.
      manual         locked to the player picked with P.
    """

    DEFAULT_PREFER = ['spotify', 'spotifyd', 'spotify_player', 'ncspot', 'tidal-hifi',
                      'cider', 'amberol', 'rhythmbox', 'strawberry', 'audacious',
                      'clementine', 'deadbeef', 'lollypop', 'vlc', 'mpv']
    BROWSERS = ('firefox', 'chromium', 'chrome', 'google-chrome', 'brave', 'vivaldi',
                'opera', 'edge', 'msedge', 'librewolf', 'zen', 'floorp', 'epiphany',
                'plasma-browser-integration', 'kdeconnect', 'waterfox', 'thorium')
    FMT_KEYS = ('playerName', 'playerInstance', 'artist', 'title', 'album',
                'status', 'position', 'mpris:length', 'mpris:artUrl', 'volume')

    def __init__(self, prefer=None, lock=None, mode='priority', poll=1.0):
        self.ok = bool(shutil.which('playerctl'))
        self.prefer = [p.lower() for p in (prefer or [])] + self.DEFAULT_PREFER
        self.lock = lock.lower() if lock else None
        self.mode = mode
        self.poll = poll
        self.track = None
        self.players = []
        self.ev = threading.Event()
        # optimistic values waiting for the next poll to confirm them
        self.pend = {}
        self.alive = True
        if self.ok:
            threading.Thread(target=self._run, daemon=True).start()

    def rank(self, p):
        n = p.name.lower()
        if n in self.prefer:
            return self.prefer.index(n)
        if any(b in n for b in self.BROWSERS):
            return 10000
        return 500

    def choose(self, players):
        if self.lock:
            hit = [p for p in players if self.lock in (p.inst.lower(), p.name.lower())]
            if hit:
                return hit[0]
        pool = [p for p in players if p.has_info]
        if not pool:
            return None
        if self.mode == 'playing-first':
            pool = [p for p in pool if p.playing] or pool
        return min(pool, key=lambda p: (self.rank(p), not p.playing))

    def _fetch(self):
        fmt = '\t'.join('{{%s}}' % k for k in self.FMT_KEYS)
        try:
            out = subprocess.run(['playerctl', '-a', 'metadata', '--format', fmt],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        now, res = time.time(), []
        for line in out.splitlines():
            o = line.split('\t')
            o += [''] * (len(self.FMT_KEYS) - len(o))
            if len(o) < len(self.FMT_KEYS):
                continue
            try:
                pos = float(o[6]) / 1e6
            except ValueError:
                pos = 0.0
            try:
                ln = float(o[7]) / 1e6
            except ValueError:
                ln = 0.0
            try:
                vol = float(o[9])
            except ValueError:
                vol = -1.0
            res.append(Track(o[0], o[1], o[2], o[3], o[4], o[5], pos, ln, now, o[8].strip(), vol))
        return res

    def _query(self, t, what):
        try:
            return subprocess.run(['playerctl', '-p', t.inst or t.name, what], capture_output=True,
                                  text=True, timeout=1.5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ''

    def _overlay(self, t):
        """Apply optimistic values the player hasn't reported back yet."""
        if t is None:
            return None
        now, kw = time.time(), {}
        for key, (vals, exp) in list(self.pend.items()):
            if exp < now:
                del self.pend[key]
            else:
                kw.update(vals)
        return t.copy(**kw) if kw else t

    def _run(self):
        while self.alive:
            players = self._fetch()
            self.players = players
            t = self.choose(players)
            if t is not None:
                # TODO: fold these two into the main metadata query
                t.loop = self._query(t, 'loop')
                t.shuffle = self._query(t, 'shuffle').lower() == 'on'
            self.track = self._overlay(t)
            self.ev.wait(self.poll)
            self.ev.clear()

    def refresh(self, delay=.2):
        threading.Timer(delay, self.ev.set).start()

    def close(self):
        self.alive = False
        self.ev.set()

    def _cmd(self, *args):
        t = self.track
        if not self.ok or t is None:
            return False
        try:
            subprocess.Popen(['playerctl', '-p', t.inst or t.name, *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        self.refresh()
        return True

    def _opt(self, key, **kw):
        # show the expected state immediately; the poll confirms it shortly
        self.pend[key] = (kw, time.time() + 1.2)
        if self.track is not None:
            self.track = self.track.copy(**kw)

    def toggle(self):
        t = self.track
        ok = self._cmd('play-pause')
        if ok and t is not None:
            self._opt('status', status='Paused' if t.playing else 'Playing',
                      pos=t.position(), stamp=time.time())
        return ok

    def next(self):
        return self._cmd('next')

    def prev(self):
        return self._cmd('previous')

    def seek(self, seconds):
        t = self.track
        sign = '+' if seconds >= 0 else '-'
        ok = self._cmd('position', '%g%s' % (abs(seconds), sign))
        if ok and t is not None:
            new = max(0.0, t.position() + seconds)
            self._opt('pos', pos=min(new, t.ln) if t.ln > 0 else new, stamp=time.time())
        return ok

    def seek_to(self, sec):
        t = self.track
        sec = max(0.0, sec)
        ok = self._cmd('position', '%.2f' % sec)
        if ok and t is not None:
            self._opt('pos', pos=sec, stamp=time.time())
        return ok

    def set_volume(self, v):
        v = clamp(v, 0.0, 1.0)
        ok = self._cmd('volume', '%.2f' % v)
        if ok:
            self._opt('vol', vol=v)
        return ok

    def volume_step(self, delta):
        t = self.track
        if t is None or t.vol < 0:
            return False
        return self.set_volume(t.vol + delta)

    def shuffle_toggle(self):
        t = self.track
        if t is None:
            return False
        new = not t.shuffle
        ok = self._cmd('shuffle', 'On' if new else 'Off')
        if ok:
            self._opt('shuffle', shuffle=new)
        return ok

    LOOPS = {'None': 'Playlist', 'Playlist': 'Track', 'Track': 'None'}

    def loop_cycle(self):
        t = self.track
        if t is None:
            return False
        new = self.LOOPS.get(t.loop, 'Playlist')
        ok = self._cmd('loop', new)
        if ok:
            self._opt('loop', loop=new)
        return ok

    def select(self, inst):
        """Lock the HUD to one player, or pass None for auto selection."""
        self.lock = inst.lower() if inst else None
        self.track = self._overlay(self.choose(self.players))
        self.refresh(.05)
        return inst or 'auto'

    def cycle(self):
        """auto -> player 1 -> player 2 -> ... -> auto."""
        names = [p.inst or p.name for p in self.players]
        if not names:
            return 'no players found'
        low = [None] + [n.lower() for n in names]
        cur = self.lock if self.lock in low else None
        nxt = ([None] + names)[(low.index(cur) + 1) % len(low)]
        return self.select(nxt)


# Keyboard and mouse input

def parse_input(data):
    """Turn raw terminal input into ('key', name) / ('mouse', b, c, r, kind) events."""
    ev = []
    i, n = 0, len(data)
    while i < n:
        ch = data[i]
        if ch == '\x1b':
            m = MOUSE_RE.match(data, i)
            if m:
                ev.append(('mouse', int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)))
                i = m.end()
                continue
            m = CSI_RE.match(data, i)
            if m:
                params, final = m.groups()
                if final == '~':
                    name = TILDE_KEYS.get(params.split(';')[0])
                else:
                    name = CSI_KEYS.get(final)
                if name:
                    if params.endswith(';2') or params.endswith(';6'):
                        name = 'S-' + name
                    ev.append(('key', name))
                i = m.end()
                continue
            m = SS3_RE.match(data, i)
            if m:
                name = CSI_KEYS.get(m.group(1))
                if name:
                    ev.append(('key', name))
                i = m.end()
                continue
            ev.append(('key', 'esc'))
        elif ch in '\r\n':
            ev.append(('key', 'enter'))
        elif ch == '\t':
            ev.append(('key', 'tab'))
        elif ch in '\x7f\x08':
            ev.append(('key', 'bksp'))
        elif ch == '\x03':
            ev.append(('key', 'ctrl-c'))
        elif ch >= ' ':
            ev.append(('key', ch))
        i += 1
    return ev


# Album art colours

class ArtColors:
    """Downloads album art and extracts two vivid colours. Runs in a
    background thread; needs Pillow."""

    available = None

    def __init__(self):
        self.url = ''
        self.pair = None
        self.img = None
        self._ckey = ()
        self._crows = []

    @classmethod
    def check(cls):
        if cls.available is None:
            try:
                import PIL  # noqa: F401
                cls.available = True
            except ImportError:
                cls.available = False
        return bool(cls.available)

    def request(self, url):
        if url == self.url:
            return
        self.url = url
        self.img = None
        if not url:
            self.pair = None
            return
        if self.check():
            threading.Thread(target=self._work, args=(url,), daemon=True).start()

    def _work(self, url):
        pair = None
        try:
            from PIL import Image
            full = Image.open(io.BytesIO(self._load(url))).convert('RGB')
            small = full.resize((48, 48))
            pair = self.extract(np.asarray(small, dtype=np.float32).reshape(-1, 3))
            if url == self.url:
                self.img = full.resize((160, 160), getattr(Image, 'Resampling', Image).LANCZOS)
        except Exception:
            pair = None
        if url == self.url and pair is not None:
            self.pair = pair

    def cover_rows(self, w, h):
        """The cover as h rows of w half-block cells (two pixels per cell), or None."""
        img = self.img
        if img is None:
            return None
        key = (id(img), w, h)
        if key != self._ckey:
            from PIL import Image
            arr = np.asarray(img.resize((w, h * 2), getattr(Image, 'Resampling', Image).LANCZOS), dtype=np.uint8)
            self._crows = half_blocks(arr)
            self._ckey = key
        return self._crows

    @staticmethod
    def _load(url):
        if url.startswith('file://'):
            with open(urllib.parse.unquote(url[7:]), 'rb') as fh:
                return fh.read(5_000_000)
        if url.startswith(('http://', 'https://')):
            req = urllib.request.Request(url, headers={'User-Agent': 'vizr'})
            with urllib.request.urlopen(req, timeout=4) as r:
                return r.read(3_000_000)
        raise ValueError('unsupported art url')

    @staticmethod
    def boost(c):
        m = max(c)
        if m < 1:
            return 200, 200, 200
        k = 235.0 / m
        return int(min(255, c[0] * k)), int(min(255, c[1] * k)), int(min(255, c[2] * k))

    @classmethod
    def extract(cls, px):
        mx, mn = px.max(1), px.min(1)
        score = (mx - mn) / np.maximum(mx, 1) * (mx / 255.0) ** 1.2
        hues = np.array([colorsys.rgb_to_hsv(*(p / 255.0))[0] for p in px])
        bins = (hues * 12).astype(int) % 12
        weight = np.bincount(bins, weights=score, minlength=12)
        if weight.max() < 1.0:                           # grey / monochrome cover
            lum = px.mean(1)
            top = px[lum >= np.percentile(lum, 70)].mean(0)
            return cls.boost(top), (130, 150, 185)
        b1 = int(weight.argmax())
        m1 = (bins == b1)
        c1 = (px[m1] * score[m1, None]).sum(0) / max(score[m1].sum(), 1e-6)
        far = np.array([min((i - b1) % 12, (b1 - i) % 12) >= 2 for i in range(12)])
        w2 = np.where(far, weight, 0)
        if w2.max() > weight.max() * .15:
            b2 = int(w2.argmax())
            m2 = (bins == b2)
            c2 = (px[m2] * score[m2, None]).sum(0) / max(score[m2].sum(), 1e-6)
            return cls.boost(c1), cls.boost(c2)
        c1 = cls.boost(c1)
        return c1, rotate_hue(c1, .12)


def half_blocks(arr):
    """Render an (2h, w, 3) uint8 image as h rows of half-block cells."""
    rows = []
    for r in range(arr.shape[0] // 2):
        top, bot = arr[2 * r].tolist(), arr[2 * r + 1].tolist()
        rows.append(''.join('\x1b[38;2;%d;%d;%d;48;2;%d;%d;%dm\u2580' % (a[0], a[1], a[2], b[0], b[1], b[2])
                            for a, b in zip(top, bot)) + RESET)
    return rows


def placeholder_cover(w, h, c1, c2):
    """Vinyl-style gradient for when there's no cover art."""
    ys, xs = np.mgrid[0:h * 2, 0:w]
    d = np.hypot(xs - w / 2, ys - h)
    ring = (.5 + .5 * np.sin(d * .6))[..., None]
    g = np.clip((xs / max(w, 1) + ys / max(h * 2, 1)) / 2, 0, 1)[..., None]
    base = np.array(c1) * (1 - g) + np.array(c2) * g
    arr = base * (.18 + .5 * ring)
    arr[d < max(1.5, w / 12)] = (14, 16, 26)
    return half_blocks(np.clip(arr, 0, 255).astype(np.uint8))


# Help text

HELP_SECTIONS = [
    ('Playback', [
        ('Space / K', 'Play / pause'),
        (', and .', 'Previous / next track'),
        ('[ and ]', 'Seek back / forward'),
        ('{ and }', 'Seek 3x further'),
        ('L', 'Loop: off / list / track'),
        ('Z', 'Shuffle on / off'),
        ('V / Shift+V', 'Volume up / down'),
        ('P', 'Cycle through players'),
        ('Mouse', 'Click buttons, bars, players'),
    ]),
    ('Visuals', [
        ('1-9, 0', 'First ten modes'),
        ('M or Right', 'Next mode (24 total)'),
        ('Shift+M or Left', 'Previous mode'),
        ('T / Shift+T', 'Next / previous theme'),
        ('C / Shift+C', 'Next / previous accent'),
        ('Y', 'Kaleidoscope symmetry'),
        ('G', 'Album-art colours'),
        ('I', 'Info sidebar'),
        ('A', 'Toggle auto-cycle'),
        ('Shift+H', 'Show / hide the HUD'),
        ('F', 'Performance readout'),
    ]),
    ('Particles & audio', [
        ('Up / Down', 'More / fewer particles'),
        ('+ / -', 'Sensitivity'),
        ('Mouse move', 'Push particles away'),
        ('Left click', 'Shockwave (push)'),
        ('Right click', 'Shockwave (pull)'),
        ('Mouse wheel', 'Pointer radius'),
    ]),
    ('Interface', [
        ('S or Tab', 'Settings panel'),
        ('? or H', 'This help screen'),
        ('W', 'Save settings now'),
        ('Q', 'Quit'),
        ('In settings', 'Arrows adjust, Shift coarse'),
        ('', 'Enter toggle, 0 reset'),
        ('', 'Tab or 1-9 for sections'),
    ]),
]

DIGITS = tuple('1234567890')


# Main application

class App:
    """Everything lives here: input, layout, drawing, actions, the loop.

    One big object on purpose - the state really is shared, and splitting it
    up would just mean passing the same dicts around. frame() is the only
    orchestrator."""

    def __init__(self, args):
        self.args = args
        path = None if args.no_config else (args.config or default_config_path())
        self.cfg = Config(path)
        if not args.reset_config:
            self.cfg.load()
        self.theme_guard = False
        self.apply_cli(args)
        self.cfg.take_dirty()

        P = self.cfg.v
        self.forced_demo = bool(args.demo)
        self.audio_source = self.eff_source()
        self.audio = Audio(self.audio_source, args.device, args.monitor)
        prefer = [p.strip() for p in (args.prefer or '').split(',') if p.strip()]
        prefer += self.cfg.meta.get('prefer', [])
        self.media = Media(prefer, args.player, P['player_mode'], P['poll'])
        self.spec = Spec()
        self.art = ArtColors()
        self.field = None

        self.fd = sys.stdin.fileno()
        self.out = sys.stdout
        self.old_attrs = None
        self.running = True
        self.mouse_on = False

        self.layout_key = None
        self.size = (80, 24)
        self.top = 0
        self.lut = []
        self.lut_key = None
        self.bg_name = ''
        self.bg = ''
        self.A = fg(*ACCENTS[P['accent']])
        self.bg_now = ''
        self.cur_cols = None
        self.flash = 0.0
        self.silent = 0.0
        self.lv = np.zeros(8)
        self.dt = .02
        self.clock = 0.0

        self.hot = []                       # clickable regions: x0, x1, y, action, arg
        self.hot_drag = None
        self.last_vol = 0.0
        self.side_w = 0
        self.fcols = 80
        self.fx0 = 0
        self._ph = ()
        self._ph_rows = []

        self.mp = [-1e4, -1e4]
        self.overlay = None
        self.tab = 0
        self.sel = {t: 0 for t in TABS}
        self.scroll = {t: 0 for t in TABS}
        self.help_scroll = 0
        self.drag = None
        self.toast_msg = None
        self.reset_armed = 0.0

        self.ui_rect = (0, 0, 0, 0)
        self.ui_rows = {}
        self.ui_tabs = []
        self.ui_tab_y = 0
        self.ui_bar = (0, 1)

        self.bavg, self.blast = .1, 0.0
        self.quality = 1.0
        self.ft = 0.0
        self.fps_now = float(P['fps'])
        self.next_cycle = 0.0
        self.morph_start = 0.0

    def apply_cli(self, a):
        c = self.cfg
        if a.theme:
            name = next((t for t in THEMES if t.lower() == a.theme.lower()), None)
            if name:
                c.set('theme', name)
                self.apply_theme()
        if a.mode:
            if a.mode.isdigit() and 1 <= int(a.mode) <= len(MODES):
                c.set('mode', MODES[int(a.mode) - 1])
            elif a.mode.lower() in MODES:
                c.set('mode', a.mode.lower())
        if a.fps:
            c.set('fps', a.fps)
        if a.density:
            c.set('density', a.density)
        if a.count:
            c.set('count_mode', 'manual')
            c.set('count', a.count)
        if a.transparent:
            c.set('background', 'transparent')
        if a.no_mouse:
            c.set('mouse', False)

    def apply_theme(self):
        acc, acc2, cmode, bgn = THEMES[self.cfg['theme']]
        self.cfg.apply({'accent': acc, 'accent2': acc2, 'color_mode': cmode, 'background': bgn})

    def eff_source(self):
        return 'demo' if getattr(self, 'forced_demo', self.args.demo) else self.cfg['source']

    def target_count(self, pw, ph):
        P = self.cfg.v
        if P['count_mode'] == 'manual':
            return int(clamp(P['count'], 1000, 250000))
        return int(clamp(pw * ph / 3.2 * P['density'], 1000, P['max_particles']))

    def term_on(self):
        self.old_attrs = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.out.write('\x1b[?1049h\x1b[?25l\x1b[2J')
        self.sync_mouse()
        self.out.flush()

    def term_off(self):
        try:
            self.out.write('\x1b[?1003l\x1b[?1006l\x1b[?25h\x1b[?1049l\x1b[0m')
            self.out.flush()
        finally:
            if self.old_attrs is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_attrs)

    def sync_mouse(self):
        want = bool(self.cfg['mouse'])
        if want != self.mouse_on:
            self.out.write('\x1b[?1003h\x1b[?1006h' if want else '\x1b[?1003l\x1b[?1006l')
            self.mouse_on = want
            if not want:
                self.mp = [-1e4, -1e4]

    def layout(self):
        P = self.cfg.v
        sz = shutil.get_terminal_size()
        C, R = max(sz.columns, 24), max(sz.lines, 10)
        hud_rows = 2 if P['hud'] else 0
        top = 2 if (P['hud'] and P['hud_pos'] == 'top') else 0
        rows = R - hud_rows
        sw = int(clamp(P['side_width'], 26, C // 2)) if (P['side_panel'] and C >= 70) else 0
        fcols = C - sw
        nb = P['bands'] or int(max(16, min(fcols * 2 // 5, 90)))
        nb = max(4, int(nb))
        key = (C, R, hud_rows, top, sw, P['side_pos'])
        pw, ph = fcols * 2, rows * 4
        if key != self.layout_key or self.field is None or self.field.nb != nb:
            self.layout_key = key
            self.size, self.top = (C, R), top
            self.side_w, self.fcols = sw, fcols
            self.fx0 = sw if (sw and P['side_pos'] == 'left') else 0
            self.field = Field(fcols, rows, self.target_count(pw, ph), nb)
            self.field.set_quality(self.quality)
            self.out.write('\x1b[2J')
        else:
            n = self.target_count(pw, ph)
            if n != self.field.N:
                self.field.resize(n)
                self.field.set_quality(self.quality)
        self.spec.configure(nb, P['fmin'], P['fmax'], P['tilt'])

    def refresh_style(self):
        """Ease the current colour pair toward the target and rebuild the LUT."""
        P = self.cfg.v
        rainbow = P['color_mode'] == 'rainbow'
        tgt = (ACCENTS[P['accent']], ACCENTS[P['accent2']])
        if P['art_colors'] and self.art.pair and not rainbow:
            tgt = self.art.pair
        if self.cur_cols is None:
            self.cur_cols = tgt
        else:
            k = 1 - math.exp(-self.dt * 4)
            self.cur_cols = tuple(tuple(c + (g - c) * k for c, g in zip(a, b))
                                  for a, b in zip(self.cur_cols, tgt))
        off = 0.0
        if P['hue_speed'] > 0:
            off = int(self.clock * P['hue_speed'] * .05 * 96 % 96) / 96
        c1 = tuple(int(v) for v in self.cur_cols[0])
        c2 = tuple(int(v) for v in self.cur_cols[1])
        key = (c1, c2, P['brightness'], P['glow'], rainbow, off)
        if key != self.lut_key:
            self.lut_key = key
            if rainbow:
                bases = rainbow_bases(off)
            else:
                if off:
                    c1, c2 = rotate_hue(c1, off), rotate_hue(c2, off)
                bases = lerp_bases(c1, c2)
            self.lut = build_lut(bases, P['brightness'], P['glow'])
            self.A = fg(*(bases[HUE_STEPS // 6] if rainbow else c1))
        if P['background'] != self.bg_name:
            self.bg_name = P['background']
            self.bg = BACKGROUNDS[self.bg_name]
            self.out.write('\x1b[2J')

    def effective_color_mode(self):
        P = self.cfg.v
        if P['color_mode'] == 'energy' and P['art_colors'] and self.art.pair:
            return 'gradient'
        return P['color_mode']

    def update_bg(self):
        """Background colour with the beat flash mixed in."""
        P = self.cfg.v
        base = BACKGROUND_RGB.get(self.bg_name)
        if base is None or P['flash'] <= .01 or self.flash < .01 or self.cur_cols is None:
            self.bg_now = self.bg
            return
        k = self.flash * P['flash'] * .16
        c = self.cur_cols[0]
        self.bg_now = bgc(*[int(clamp(base[i] + (c[i] - base[i]) * k, 0, 255)) for i in range(3)])

    def apply_dirty(self):
        dirty = self.cfg.take_dirty()
        if 'theme' in dirty:
            if not self.theme_guard:
                self.apply_theme()
            self.theme_guard = False
            self.cfg.take_dirty()
        src = self.eff_source()
        if src != self.audio_source:
            self.audio_source = src
            self.audio.start(src)
            self.toast('Audio: ' + self.audio.label)
        self.sync_mouse()
        m = self.media
        m.poll = self.cfg['poll']
        m.mode = self.cfg['player_mode']

    def adapt_quality(self, frame_time):
        P = self.cfg.v
        self.ft = self.ft * .9 + frame_time * .1
        if not P['adaptive']:
            self.quality = 1.0
        else:
            budget = 1.0 / P['fps']
            if self.ft > budget * .97:
                self.quality = max(P['min_quality'], self.quality * .97)
            elif self.ft < budget * .7 and self.quality < 1:
                self.quality = min(1.0, self.quality * 1.015)
        if self.field is not None:
            self.field.set_quality(self.quality)

    def toast(self, msg, dur=1.8):
        if self.cfg['toasts']:
            self.toast_msg = (msg, time.time() + dur)

    def read_input(self):
        shocks = []
        data = ''
        while select.select([self.fd], [], [], 0)[0]:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                break
            data += chunk.decode('utf8', 'ignore')
        if not data:
            return shocks
        for ev in parse_input(data):
            if ev[0] == 'mouse':
                self.on_mouse(ev[1], ev[2], ev[3], ev[4], shocks)
            else:
                self.on_key(ev[1])
        return shocks

    def in_panel(self, c, r):
        x0, y0, w, h = self.ui_rect
        return self.overlay is not None and x0 <= c < x0 + w and y0 <= r < y0 + h

    def in_side(self, c):
        if not self.side_w:
            return False
        C = self.size[0]
        return c <= self.side_w if self.cfg['side_pos'] == 'left' else c > C - self.side_w

    def hot_at(self, c, r):
        for h in self.hot:
            if h[2] == r and h[0] <= c <= h[1]:
                return h
        return None

    def hot_act(self, act, arg, c, x0, x1):
        m = self.media
        frac = clamp((c - x0) / max(x1 - x0, 1), 0, 1)
        if act == 'play':
            self.media_do('toggle')
        elif act == 'prev':
            self.media_do('prev')
        elif act == 'next':
            self.media_do('next')
        elif act == 'shuffle':
            self.media_do('shuffle_toggle')
        elif act == 'loop':
            self.media_do('loop_cycle')
        elif act == 'seek':
            tr = m.track
            if tr is not None and tr.ln > 0:
                m.seek_to(frac * tr.ln)
        elif act == 'vol':
            m.set_volume(frac)
        elif act == 'auto':
            self.toast('Player: auto (priority)')
            m.select(None)
        elif act == 'player':
            if m.lock == str(arg).lower():
                m.select(None)
                self.toast('Player: auto (priority)')
            else:
                self.toast('Player locked: ' + str(arg))
                m.select(str(arg))

    def on_mouse(self, b, c, r, kind, shocks):
        if self.overlay and (self.in_panel(c, r) or self.drag is not None):
            self.mp = [-1e4, -1e4]
            if self.overlay == 'settings':
                self.panel_mouse(b, c, r, kind)
            elif b in (64, 65):
                self.help_scroll = max(0, self.help_scroll + (-1 if b == 64 else 1))
            return
        if not self.overlay:
            if kind == 'm':
                if self.hot_drag is not None:
                    act, x0, x1 = self.hot_drag
                    self.hot_drag = None
                    self.hot_act(act, None, c, x0, x1)
                    return
            elif self.hot_drag is not None and (b & 32) and (b & 3) == 0:
                act, x0, x1 = self.hot_drag
                now = time.time()
                if act == 'vol' and now - self.last_vol > .12:
                    self.last_vol = now
                    self.hot_act(act, None, c, x0, x1)
                return
            elif b < 64 and not b & 32 and (b & 3) == 0:
                h = self.hot_at(c, r)
                if h is not None:
                    x0, x1, _y, act, arg = h
                    if act in ('seek', 'vol'):
                        self.hot_drag = (act, x0, x1)
                        if act == 'vol':
                            self.hot_act(act, arg, c, x0, x1)
                    else:
                        self.hot_act(act, arg, c, x0, x1)
                    return
            if self.in_side(c):
                self.mp = [-1e4, -1e4]
                if b in (64, 65):
                    step = self.cfg['volume_step'] / 100.0
                    self.media_do('volume_step', step if b == 64 else -step)
                return
        self.mp = [(c - 1 - self.fx0) * 2 + 1, (r - 1 - self.top) * 4 + 2]
        if b >= 64:
            v = self.cfg['mouse_radius'] * (1.12 if b == 64 else 1 / 1.12)
            self.cfg.set('mouse_radius', v)
        elif not b & 32 and kind == 'M':
            shocks.append((self.mp[0], self.mp[1], (b & 3) == 0))

    def on_key(self, name):
        if name == 'ctrl-c':
            self.running = False
        elif self.overlay == 'settings':
            self.settings_key(name)
        elif self.overlay == 'help':
            self.help_key(name)
        else:
            self.global_key(name)

    def open_overlay(self, which):
        self.overlay = which
        self.drag = None
        self.mp = [-1e4, -1e4]
        self.help_scroll = 0
        self.out.write('\x1b[2J')

    def close_overlay(self):
        if self.overlay == 'help':
            self.cfg.meta['seen_help'] = True
        self.overlay = None
        self.drag = None
        self.out.write('\x1b[2J')

    def set_mode(self, i, announce=True):
        name = MODES[i % len(MODES)]
        if name == self.cfg['mode']:
            return
        self.cfg.set('mode', name)
        # freeze the old shape so the next frames can glide into the new one
        if self.field is not None:
            self.field.snap()
        self.morph_start = time.time()
        if announce:
            self.toast('Mode: ' + name)

    def step_mode(self, d, announce=True):
        self.set_mode(MODES.index(self.cfg['mode']) + d, announce)

    def bump_particles(self, d):
        key = 'count' if self.cfg['count_mode'] == 'manual' else 'density'
        self.cfg.adjust(key, d, 2)
        if self.field is not None:
            n = self.target_count(self.field.PW, self.field.PH)
            self.toast('Particles: {:,}'.format(n))

    LOOP_NAMES = {'None': 'off', 'Playlist': 'playlist', 'Track': 'single track'}

    def media_do(self, what, *args):
        if not self.media.ok:
            self.toast('Install playerctl for media controls')
            return
        if not getattr(self.media, what)(*args):
            if what == 'volume_step':
                self.toast('This player does not report a volume')
            else:
                self.toast('No active player to control')
            return
        tr = self.media.track
        if tr is None:
            return
        if what == 'toggle':
            self.toast('Playing' if tr.playing else 'Paused', 1.0)
        elif what == 'loop_cycle':
            self.toast('Loop: ' + self.LOOP_NAMES.get(tr.loop, tr.loop), 1.2)
        elif what == 'shuffle_toggle':
            self.toast('Shuffle: ' + ('on' if tr.shuffle else 'off'), 1.2)
        elif what in ('volume_step', 'set_volume'):
            self.toast('Volume: %d%%' % round(tr.vol * 100), 1.0)

    def cycle_player(self):
        label = self.media.cycle()
        self.toast('Player: ' + label)

    def global_key(self, name):
        cfg, P = self.cfg, self.cfg.v
        if name == 'q':
            self.running = False
        elif name in ('?', 'h'):
            self.open_overlay('help')
        elif name in ('s', 'tab'):
            if self.size[1] < 14 or self.size[0] < 40:
                self.toast('Terminal too small for settings (need 40x14)')
            else:
                self.open_overlay('settings')
        elif name in DIGITS:
            self.set_mode((int(name) - 1) % 10)
        elif name in ('m', 'right'):
            self.step_mode(1)
        elif name in ('M', 'left'):
            self.step_mode(-1)
        elif name in (' ', 'k'):
            self.media_do('toggle')
        elif name in ('.', '>'):
            self.media_do('next')
        elif name in (',', '<'):
            self.media_do('prev')
        elif name in (']', '[', '}', '{'):
            self.media_do('seek', {'[': -1, ']': 1, '{': -3, '}': 3}[name] * P['seek_step'])
        elif name == 'l':
            self.media_do('loop_cycle')
        elif name == 'z':
            self.media_do('shuffle_toggle')
        elif name in ('v', 'V'):
            step = P['volume_step'] / 100.0
            self.media_do('volume_step', step if name == 'v' else -step)
        elif name == 'i':
            cfg.adjust('side_panel', 1)
            if P['side_panel'] and self.size[0] < 70:
                self.toast('Terminal too narrow for the sidebar (need 70 columns)')
            else:
                self.toast('Info sidebar: ' + ('on' if P['side_panel'] else 'off'))
        elif name == 'p':
            self.cycle_player()
        elif name in ('c', 'C'):
            cfg.adjust('accent', 1 if name == 'c' else -1)
            self.toast('Accent: ' + P['accent'])
        elif name in ('t', 'T'):
            cfg.adjust('theme', 1 if name == 't' else -1)
            self.toast('Theme: ' + P['theme'])
        elif name in ('+', '='):
            cfg.set('sens', P['sens'] * 1.1)
            self.toast('Sensitivity: %.2f' % P['sens'], 1.0)
        elif name in ('-', '_'):
            cfg.set('sens', P['sens'] / 1.1)
            self.toast('Sensitivity: %.2f' % P['sens'], 1.0)
        elif name == 'up':
            self.bump_particles(1)
        elif name == 'down':
            self.bump_particles(-1)
        elif name == 'y':
            cfg.adjust('symmetry', 1)
            self.toast('Symmetry: ' + P['symmetry'])
        elif name == 'g':
            cfg.adjust('art_colors', 1)
            if P['art_colors'] and not ArtColors.check():
                self.toast('Album colours need Pillow:  pip install pillow', 3.0)
            else:
                self.toast('Album colours: ' + ('on' if P['art_colors'] else 'off'))
        elif name == 'a':
            cfg.adjust('autocycle', 1)
            self.toast('Auto-cycle: ' + ('on' if P['autocycle'] else 'off'))
        elif name == 'H':
            cfg.adjust('hud', 1)
        elif name == 'f':
            cfg.adjust('show_fps', 1)
        elif name == 'w':
            self.run_action('act_save')

    def rows(self):
        return tab_settings(TABS[self.tab])

    def sel_move(self, d):
        tab = TABS[self.tab]
        n = len(self.rows())
        self.sel[tab] = int(clamp(self.sel[tab] + d, 0, n - 1))

    def activate(self, st):
        if st.kind == 'action':
            self.run_action(st.key)
        elif st.kind == 'link':
            self.open_link(st)
        elif st.kind in ('toggle', 'choice'):
            self.cfg.adjust(st.key, 1)

    def open_link(self, st):
        if open_url(st.url):
            self.toast('Opened ' + st.url, 2.5)
        else:
            self.toast('No browser found - ' + st.url, 4.0)

    def settings_key(self, name):
        tab = TABS[self.tab]
        rows = self.rows()
        st = rows[self.sel[tab]]
        mult = 1
        if name.startswith('S-'):
            name, mult = name[2:], 10
        if name in ('esc', 's'):
            self.close_overlay()
        elif name == 'q':
            self.running = False
        elif name == 'up':
            self.sel_move(-1)
        elif name == 'down':
            self.sel_move(1)
        elif name == 'pgup':
            self.sel_move(-5)
        elif name == 'pgdn':
            self.sel_move(5)
        elif name == 'home':
            self.sel[tab] = 0
        elif name == 'end':
            self.sel[tab] = len(rows) - 1
        elif name in ('left', 'right'):
            if st.kind not in ('action', 'link'):
                self.cfg.adjust(st.key, -1 if name == 'left' else 1, mult)
        elif name in ('enter', ' '):
            self.activate(st)
        elif name == 'tab':
            self.tab = (self.tab + 1) % len(TABS)
        elif name == 'btab':
            self.tab = (self.tab - 1) % len(TABS)
        elif name in tuple('123456789') and int(name) <= len(TABS):
            self.tab = int(name) - 1
        elif name in ('0', 'bksp', 'del'):
            if st.kind not in ('action', 'link'):
                self.cfg.reset(st.key)
                self.toast('Reset: ' + st.label, 1.0)
        elif name == 'w':
            self.run_action('act_save')
        elif name == '?':
            self.open_overlay('help')

    def help_key(self, name):
        if name in ('esc', '?', 'h', 'enter', ' '):
            self.close_overlay()
        elif name == 'q':
            self.running = False
        elif name in ('up', 'pgup'):
            self.help_scroll = max(0, self.help_scroll - 1)
        elif name in ('down', 'pgdn'):
            self.help_scroll += 1
        elif name in ('s', 'tab'):
            self.open_overlay('settings')

    def set_slider_from_x(self, idx, c):
        x0, width = self.ui_bar
        rows = self.rows()
        if 0 <= idx < len(rows):
            self.cfg.set_frac(rows[idx].key, (c - x0) / max(width - 1, 1))

    def panel_mouse(self, b, c, r, kind):
        tab = TABS[self.tab]
        rows = self.rows()
        if b in (64, 65):
            self.sel_move(-1 if b == 64 else 1)
            return
        if kind == 'm':
            self.drag = None
            return
        if b & 32:
            if self.drag is not None:
                self.set_slider_from_x(self.drag, c)
            return
        if (b & 3) != 0:
            return
        if r == self.ui_tab_y:
            for x0, x1, i in self.ui_tabs:
                if x0 <= c <= x1:
                    self.tab = i
            return
        idx = self.ui_rows.get(r)
        if idx is None or idx >= len(rows):
            return
        st = rows[idx]
        self.sel[tab] = idx
        x0, width = self.ui_bar
        if st.kind in ('slider', 'int'):
            if x0 - 1 <= c <= x0 + width:
                self.drag = idx
                self.set_slider_from_x(idx, c)
        elif st.kind == 'choice':
            mid = x0 + (width + 11) // 2
            self.cfg.adjust(st.key, -1 if c < mid else 1)
        else:
            self.activate(st)

    def apply_preset(self, name):
        self.cfg.reset_tabs(PRESET_TABS)
        self.cfg.apply(PRESETS[name])
        self.toast('Preset: ' + name)

    def run_action(self, key):
        cfg = self.cfg
        if key == 'act_restart_audio':
            self.audio.start(self.eff_source())
            self.toast('Audio: ' + self.audio.label, 2.5)
        elif key == 'act_cycle_player':
            self.cycle_player()
        elif key in ('act_play', 'act_prev', 'act_next', 'act_shuffle', 'act_loop'):
            self.media_do({'act_play': 'toggle', 'act_prev': 'prev', 'act_next': 'next',
                           'act_shuffle': 'shuffle_toggle', 'act_loop': 'loop_cycle'}[key])
        elif key in ('act_seek_back', 'act_seek_fwd'):
            self.media_do('seek', cfg['seek_step'] * (-1 if key == 'act_seek_back' else 1))
        elif key in ('act_vol_up', 'act_vol_down'):
            self.media_do('volume_step', cfg['volume_step'] / 100.0 * (1 if key == 'act_vol_up' else -1))
        elif key == 'act_save':
            self.toast('Settings saved' if cfg.save() else 'Could not write config file', 2.0)
        elif key == 'act_reset':
            now = time.time()
            if now - self.reset_armed < 3:
                cfg.reset_tabs(TABS)
                self.reset_armed = 0
                self.toast('All settings reset')
            else:
                self.reset_armed = now
                self.toast('Press again within 3s to reset everything', 3.0)
        elif key.startswith('preset_'):
            self.apply_preset(key[len('preset_'):])
        elif key.startswith('save_'):
            slot = key[-1]
            cfg.slots[slot] = cfg.snapshot()
            cfg.save()
            self.toast('Saved to slot ' + slot)
        elif key.startswith('load_'):
            slot = key[-1]
            if slot in cfg.slots:
                self.theme_guard = True
                cfg.apply(cfg.slots[slot])
                self.toast('Loaded slot ' + slot)
            else:
                self.toast('Slot %s is empty - save to it first' % slot)

    def marquee(self, text, width, t):
        if tw(text) <= width:
            return text
        loop = text + '     '
        off = int(t * 5) % len(loop)
        return trunc((loop + loop)[off:off + len(loop) + width], width, '')

    def hud(self, t):
        """Two-line now-playing bar. Registers clickable regions in self.hot."""
        P = self.cfg.v
        W, R, ox = self.fcols, self.size[1], self.fx0
        A, bg = self.A, self.bg_now
        row0 = 1 if P['hud_pos'] == 'top' else R - 1

        def at(r, c):
            return '\x1b[%d;%dH' % (r, ox + c)

        l1 = at(row0, 1) + bg + '\x1b[K'
        l2 = at(row0 + 1, 1) + bg + '\x1b[K'
        tr = self.media.track
        tag = ''
        if tr is not None and P['show_source'] and tr.name:
            tag = tr.name + (' (locked)' if self.media.lock else '')
        tail_bits = []
        if tr is not None:
            chips = ('\u21c4 ' if tr.shuffle else '') + {'Playlist': '\u21bb', 'Track': '\u21bb1'}.get(tr.loop, '')
            if chips.strip():
                tail_bits.append(chips.strip())
        tail_bits.append(P['mode'])
        if P['show_fps']:
            tail_bits.append('%dfps %s %d%%' % (self.fps_now, '{:,}'.format(self.field.active if self.field else 0),
                                                 self.quality * 100))
        if P['show_hints']:
            tail_bits.append('? help')
            if W >= 70:
                tail_bits.append('s settings')
        tail = '  '.join(tail_bits)
        meter = ''
        if P['show_meter'] and W >= 60 and len(self.lv) >= 10:
            edges = np.linspace(0, len(self.lv), 11).astype(int)
            vals = [float(self.lv[a_:max(b_, a_ + 1)].mean()) for a_, b_ in zip(edges[:-1], edges[1:])]
            meter = ''.join(' \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588'[int(clamp(v, 0, 1) * 8)] for v in vals)

        if tr is not None and tr.has_info:
            icon = '\u25b6' if tr.playing else '\u2016'
            parts = [p for p in (tr.title, tr.artist, tr.album if P['show_album'] else '') if p]
            plain = '  \u00b7  '.join(parts)
            avail = W - 5 - (tw(tag) + 2 if tag else 0)
            l1 += ' %s%s ' % (A, icon)
            if tw(plain) <= avail:
                styled = [C_WHITE + tr.title]
                if tr.artist:
                    styled.append(A + tr.artist)
                if P['show_album'] and tr.album:
                    styled.append(C_DIM + tr.album)
                l1 += (RESET + bg + C_DIM + '  \u00b7  ').join(styled)
            elif P['marquee']:
                l1 += C_WHITE + self.marquee(plain, avail, t)
            else:
                l1 += C_WHITE + trunc(plain, avail)
            l1 += RESET
            if tag:
                l1 += at(row0, max(1, W - tw(tag))) + bg + C_DIM + tag
            y2, x = row0 + 1, 1
            l2 += ' '
            x += 1
            for act, sym in (('prev', '\u25c0\u25c0'), ('play', '\u25ae\u25ae' if tr.playing else '\u25b6 '),
                             ('next', '\u25b6\u25b6')):
                self.hot.append((ox + x, ox + x + 1, y2, act, None))
                l2 += (A + BOLD if act == 'play' else C_TXT) + sym + RESET + bg + ' '
                x += 3
            pos, ln = tr.position(), tr.ln
            total = fmt_time(ln) if ln > 0 else '--:--'
            ts = fmt_time(pos) + ' '
            l2 += ' %s%s' % (C_DIM, ts)
            x += 1 + tw(ts)
            if P['show_progress']:
                bw = max(8, min(60, W - x - tw(total) - tw(tail) - (13 if meter else 1) - 4))
                n = min(bw, int(bw * pos / ln)) if ln > 0 else 0
                self.hot.append((ox + x, ox + x + bw - 1, y2, 'seek', None))
                l2 += '%s%s%s%s ' % (A, '\u2501' * n, C_DIM, '\u2500' * (bw - n))
            l2 += '%s%s' % (C_DIM, total)
        else:
            if self.audio.src == 'demo' and not (tr and tr.has_info):
                msg = self.audio.label
            elif not self.media.ok:
                msg = 'install playerctl to show the current song'
            else:
                msg = 'nothing playing'
            l1 += ' %s%s' % (C_DIM, msg)
        if meter:
            l2 += at(row0 + 1, max(1, W - tw(tail) - 11)) + bg + A + meter
        l2 += at(row0 + 1, max(1, W - tw(tail))) + bg + C_DIM + tail
        return l1 + RESET + l2 + RESET

    def draw_toast(self, now):
        if self.toast_msg is None:
            return ''
        msg, until = self.toast_msg
        if now > until:
            self.toast_msg = None
            return ''
        text = ' %s ' % trunc(msg, self.fcols - 6)
        col = self.fx0 + max(1, self.fcols - tw(text))
        return '\x1b[%d;%dH%s%s%s%s' % (self.top + 1, col, PANEL_BG, self.A + BOLD, text, RESET)

    def draw_side(self, C, R):
        """Sidebar: cover, track info, progress, transport, volume, players."""
        P = self.cfg.v
        sw = self.side_w
        right = P['side_pos'] == 'right'
        xs = C - sw + 1 if right else 1
        cx0 = xs + 2 if right else 2
        cwid = sw - 3
        A = self.A
        tr = self.media.track
        cc = self.cur_cols[0] if self.cur_cols else ACCENTS[P['accent']]
        rows = []

        def seg_row(segs):
            rows.append(fit(segs, cwid, PANEL_BG))

        def hot(dx0, dx1, act, arg=None):
            self.hot.append((cx0 + dx0, cx0 + dx1, len(rows) + 1, act, arg))

        tag = tr.name if tr is not None else ''
        seg_row([(C_DIM + BOLD, 'NOW PLAYING'), (C_DIM, ' ' * max(1, cwid - 11 - tw(tag))), (A, tag)])
        seg_row([])

        players = self.media.players
        n_pl = (min(len(players), 3) + 1) if P['side_players'] else 0
        below = 10 + (2 + n_pl if n_pl else 0) + 1
        cover_h = min(cwid // 2, R - 2 - below)
        if cover_h >= 3:
            cover_w = min(cwid, cover_h * 2)
            crow = self.art.cover_rows(cover_w, cover_h) if P['side_cover'] else None
            if crow is None:
                key = (cover_w, cover_h, tuple(int(v) for v in cc), tuple(int(v) for v in self.cur_cols[1]) if self.cur_cols else ())
                if key != self._ph:
                    self._ph = key
                    self._ph_rows = placeholder_cover(cover_w, cover_h, cc, self.cur_cols[1] if self.cur_cols else cc)
                crow = self._ph_rows
            lp = (cwid - cover_w) // 2
            for line in crow:
                hot(lp, lp + cover_w - 1, 'play')
                rows.append(PANEL_BG + ' ' * lp + line + PANEL_BG + ' ' * (cwid - lp - cover_w) + RESET)
            if P['side_cover'] and self.art.img is None and not ArtColors.check():
                seg_row([(C_DIM, trunc('install Pillow for cover art', cwid))])
            else:
                seg_row([])

        if tr is not None and tr.has_info:
            wrapped = textwrap.wrap(tr.title or '(untitled)', cwid) or ['']
            if len(wrapped) > 2:
                wrapped = [wrapped[0], trunc(' '.join(wrapped[1:]), cwid)]
            wrapped += [''] * (2 - len(wrapped))
            for ln_ in wrapped:
                seg_row([(C_WHITE, ln_)])
            seg_row([(A, tr.artist)])
            seg_row([(C_DIM, tr.album if P['show_album'] else '')])
        else:
            seg_row([(C_DIM, 'Nothing playing')])
            seg_row([(C_DIM, 'start a song in your player')])
            seg_row([])
            seg_row([])
        seg_row([])

        pos = tr.position() if tr is not None else 0.0
        ln = tr.ln if tr is not None else 0.0
        t1 = fmt_time(pos)
        t2 = fmt_time(ln) if ln > 0 else '--:--'
        bw = max(4, cwid - tw(t1) - tw(t2) - 2)
        n = min(bw, int(bw * pos / ln)) if ln > 0 else 0
        if tr is not None:
            hot(tw(t1) + 1, tw(t1) + bw, 'seek')
        seg_row([(C_DIM, t1 + ' '), (A, '\u2501' * n), (C_DIM, '\u2500' * (bw - n) + ' ' + t2)])
        seg_row([])

        playing = tr is not None and tr.playing
        abg = '\x1b[48;2;%d;%d;%dm\x1b[1;38;2;8;12;16m' % tuple(int(v) for v in cc)
        loop_sym = {'Playlist': '\u21bb ', 'Track': '\u21bb1'}.get(tr.loop if tr else '', '\u21bb ')
        loop_on = bool(tr and tr.loop in ('Playlist', 'Track'))
        items = [
            ('shuffle', '\u21c4', A + BOLD if (tr and tr.shuffle) else C_DIM),
            ('prev', '\u25c0\u25c0', C_TXT),
            ('play', ' \u25ae\u25ae ' if playing else ' \u25b6  ', abg),
            ('next', '\u25b6\u25b6', C_TXT),
            ('loop', loop_sym, A + BOLD if loop_on else C_DIM),
        ]
        total_w = sum(tw(s) for _a, s, _st in items) + 2 * (len(items) - 1)
        x = max(0, (cwid - total_w) // 2)
        segs = [(C_DIM, ' ' * x)]
        for i, (act, sym, st) in enumerate(items):
            if tr is not None:
                hot(x, x + tw(sym) - 1, act)
            segs.append((st, sym))
            x += tw(sym)
            if i < len(items) - 1:
                segs.append((C_DIM, '  '))
                x += 2
        seg_row(segs)

        vol = tr.vol if (tr is not None and tr.vol >= 0) else -1.0
        vbw = max(4, cwid - 2 - 5)
        vn = int(vol * vbw) if vol >= 0 else 0
        if vol >= 0:
            hot(2, 2 + vbw - 1, 'vol')
        pct = ('%3d%%' % round(vol * 100)) if vol >= 0 else ' -- '
        seg_row([(C_DIM, '\u266a '), (A, '\u2501' * vn), (C_DIM, '\u2500' * (vbw - vn) + ' '), (C_TXT, pct)])

        if n_pl:
            seg_row([])
            seg_row([(C_DIM + BOLD, 'PLAYERS')])
            auto = self.media.lock is None
            hot(0, cwid - 1, 'auto')
            seg_row([(A if auto else C_DIM, '\u25cf ' if auto else '\u25cb '),
                     (C_WHITE if auto else C_TXT, 'auto'), (C_DIM, '  priority')])
            cur = (tr.inst or tr.name) if tr is not None else ''
            for p in players[:n_pl - 1]:
                inst = p.inst or p.name
                sel = bool(self.media.lock) and self.media.lock == inst.lower()
                shown = inst == cur
                hot(0, cwid - 1, 'player', inst)
                glyph = '\u25b6' if p.playing else '\u2016'
                label = pad(trunc(p.name, cwid - 6), cwid - 5)
                seg_row([(A if shown else C_DIM, '\u25cf ' if (sel or shown) else '\u25cb '),
                         (C_WHITE if sel else C_TXT, label), (A if p.playing else C_DIM, glyph)])
        while len(rows) < R - 1:
            rows.append(fit([], cwid, PANEL_BG))
        rows = rows[:R - 1]
        rows.append(fit([(C_DIM, trunc('? help  \u00b7  i hide sidebar', cwid))], cwid, PANEL_BG))
        self.hot = [h for h in self.hot if h[2] <= R]

        border = fg(*[int(v * .55) for v in cc])
        out = []
        for y, content in enumerate(rows, 1):
            if right:
                out.append('\x1b[%d;%dH%s%s\u2502%s %s%s %s' % (y, xs, RESET, border, PANEL_BG, content, PANEL_BG, RESET))
            else:
                out.append('\x1b[%d;1H%s %s%s %s\u2502%s' % (y, PANEL_BG, content, PANEL_BG, border, RESET))
        return ''.join(out)

    def box(self, w, title, entries):
        """entries: None (separator) | segs | (segs, base_style)"""
        B = PANEL_BG + fg(62, 82, 98)
        inner = w - 2
        top = RESET + B + '\u256d\u2500 ' + self.A + BOLD + title + RESET + B + \
            ' ' + '\u2500' * max(0, w - 5 - tw(title)) + '\u256e' + RESET
        out = [top]
        for e in entries:
            if e is None:
                out.append(RESET + B + '\u251c' + '\u2500' * inner + '\u2524' + RESET)
                continue
            segs, base = e if isinstance(e, tuple) else (e, PANEL_BG)
            out.append(RESET + B + '\u2502' + fit(segs, inner, base) + B + '\u2502' + RESET)
        out.append(RESET + B + '\u2570' + '\u2500' * inner + '\u256f' + RESET)
        return out

    @staticmethod
    def place(lines, x0, y0):
        return ''.join('\x1b[%d;%dH%s' % (y0 + i, x0, ln) for i, ln in enumerate(lines))

    def credits_head(self):
        """Banner above the link list. Separate so draw_settings can count the
        lines before it lays out the box."""
        A = self.A
        return [
            [(C_DIM, '')],
            [(A + BOLD, '  vizr'), (C_DIM, '   %s' % VERSION),
             (C_DIM, '   \u00b7   terminal particle visualizer')],
            [(C_DIM, '  made by '), (C_WHITE + BOLD, AUTHOR)],
            [(C_DIM, '  one file, twenty-four modes, numpy and nothing else')],
            [(C_DIM, '')],
        ]

    def draw_settings(self, C, R):
        A = self.A
        cfg = self.cfg
        tab = TABS[self.tab]
        rows = tab_settings(tab)
        maxrows = max(len(tab_settings(t)) for t in TABS)
        head = self.credits_head() if tab == 'Credits' else []
        w = min(C - 2, 96)
        inner = w - 2
        # credits has four rows of its own, so let that panel stay small
        # instead of inheriting the height of the longest tab
        room = min(maxrows, len(rows)) if tab == 'Credits' else maxrows
        body_h = max(3, min(room, R - 2 - 8 - len(head)))
        h = body_h + 8 + len(head)
        x0 = (C - w) // 2 + 1
        y0 = max(1, (R - h) // 2 + 1)

        sel = self.sel[tab] = int(clamp(self.sel[tab], 0, len(rows) - 1))
        sc = self.scroll[tab]
        if sel < sc:
            sc = sel
        if sel >= sc + body_h:
            sc = sel - body_h + 1
        sc = int(clamp(sc, 0, max(0, len(rows) - body_h)))
        self.scroll[tab] = sc

        full = sum(len(n) + 5 for n in TABS)
        names = TABS if full + 1 <= inner else TABS_SHORT
        r_, g_, b_ = ACCENTS[cfg['accent']]
        active = '\x1b[48;2;%d;%d;%dm\x1b[1;38;2;8;12;16m' % (r_, g_, b_)
        segs = [(C_DIM, ' ')]
        self.ui_tabs = []
        x = x0 + 2
        for i, n in enumerate(names):
            label = ' %d %s ' % (i + 1, n)
            self.ui_tabs.append((x, x + tw(label) - 1, i))
            segs.append((active if i == self.tab else C_DIM, label))
            segs.append((C_DIM, ' '))
            x += tw(label) + 1
        self.ui_tab_y = y0 + 1

        LW = min(26, max(12, inner // 3))
        VW = 10
        cwid = max(6, inner - 5 - LW - VW)
        self.ui_bar = (x0 + 5 + LW, cwid)
        self.ui_rows = {}

        entries = [segs] + head + [None]
        for j in range(body_h):
            idx = sc + j
            if idx >= len(rows):
                entries.append([(C_DIM, '')])
                continue
            st = rows[idx]
            on = idx == sel
            self.ui_rows[y0 + 3 + len(head) + j] = idx
            ptr = '\u25b8' if on else ' '
            line = [
                (A if on else C_DIM, ' %s ' % ptr),
                (C_WHITE if on else C_TXT, pad(st.label, LW)),
                (C_DIM, ' '),
            ]
            val = cfg.display(st.key) if st.kind not in ('action', 'link') else ''
            if st.kind in ('slider', 'int'):
                n = int(round(cfg.frac(st.key) * (cwid - 1)))
                line.append((A if on else fg(60, 120, 135), '\u2501' * n + '\u25cf'))
                line.append((C_DIM, '\u2500' * (cwid - 1 - n)))
                line.append((C_WHITE if on else C_TXT, ' ' + val.rjust(VW - 1)))
            elif st.kind == 'choice':
                line.append((C_DIM, '\u25c2 '))
                line.append((A if on else C_TXT, pad(val, cwid + VW - 3)))
                line.append((C_DIM, '\u25b8'))
            elif st.kind == 'toggle':
                state = bool(cfg[st.key])
                line.append((C_OK if state else C_DIM, ('[\u25a0] ON' if state else '[ ] off')))
            elif st.kind == 'link':
                # show the address itself - it is the interesting bit here
                url_txt = trunc(st.url.split('://')[-1].rstrip('/'), cwid + VW - 3)
                line.append((C_DIM, '\u2197 '))
                line.append((A if on else C_TXT, pad(url_txt, cwid + VW - 3)))
            else:
                line.append((A if on else C_DIM, '\u23ce  run'))
            entries.append((line, SEL_BG if on else PANEL_BG))

        entries.append(None)
        st = rows[sel]
        desc = st.desc
        if st.kind not in ('action', 'link'):
            desc += '  (default: %s)' % Config.fmt_value(st, st.default)
        wrapped = textwrap.wrap(desc, max(10, inner - 2))[:2]
        wrapped += [''] * (2 - len(wrapped))
        for ln in wrapped:
            entries.append([(C_TXT, ' ' + ln)])
        if tab == 'Credits':
            hint = ' \u2191\u2193 select   \u23ce open link   Tab section   Esc close'
        else:
            hint = (' \u2191\u2193 select  \u2190\u2192 adjust  Shift coarse  \u23ce toggle  '
                    '0 reset  Tab section  Esc close')
        entries.append([(C_DIM, hint)])
        if tab == 'Credits':
            title = 'vizr  \u00b7  credits  \u00b7  v%s' % VERSION
        else:
            title = 'ESEQ settings  \u00b7  %s' % tab
        lines = self.box(w, title, entries)
        self.ui_rect = (x0, y0, w, len(lines))
        return self.place(lines, x0, y0)

    def help_cols(self, inner):
        A = self.A + BOLD

        def sect(sec):
            name, items = sec
            out = [[(C_WHITE, name)]]
            for k, d in items:
                out.append([(A, pad(k, 16)), (C_TXT, d)])
            out.append([(C_DIM, '')])
            return out

        left = sect(HELP_SECTIONS[0]) + sect(HELP_SECTIONS[3])
        right = sect(HELP_SECTIONS[1]) + sect(HELP_SECTIONS[2])
        if inner >= 78:
            colw = inner // 2
            rows = []
            for i in range(max(len(left), len(right))):
                a = left[i] if i < len(left) else [(C_DIM, '')]
                b = right[i] if i < len(right) else [(C_DIM, '')]
                row = [(C_DIM, ' ')]
                used = 1
                for st, tx in a:
                    tx = pad(tx, min(tw(tx), colw - used - 1)) if used + tw(tx) < colw else trunc(tx, colw - used - 1, '')
                    row.append((st, tx))
                    used += tw(tx)
                row.append((C_DIM, ' ' * max(0, colw - used)))
                row.extend(b)
                rows.append(row)
            return rows
        return [[(C_DIM, ' ')] + r for r in left + right]

    def draw_help(self, C, R):
        w = min(C - 2, 84)
        inner = w - 2
        cols = self.help_cols(inner)
        body_h = max(4, min(len(cols), R - 2 - 4))
        self.help_scroll = int(clamp(self.help_scroll, 0, max(0, len(cols) - body_h)))
        view = cols[self.help_scroll:self.help_scroll + body_h]
        while len(view) < body_h:
            view.append([(C_DIM, '')])
        entries = list(view)
        entries.append(None)
        more = '  \u2191\u2193 scroll' if len(cols) > body_h else ''
        entries.append([(C_DIM, ' Esc / ? close   S settings   Q quit' + more)])
        lines = self.box(w, 'ESEQ  \u00b7  controls  \u00b7  v%s' % VERSION, entries)
        x0, y0 = (C - w) // 2 + 1, max(1, (R - len(lines)) // 2 + 1)
        self.ui_rect = (x0, y0, w, len(lines))
        return self.place(lines, x0, y0)

    def frame(self, t0, dt):
        P = self.cfg.v
        self.fps_now = self.fps_now * .92 + (1 / max(dt, 1e-3)) * .08
        self.dt, self.clock = dt, t0
        self.layout()
        shocks = self.read_input()
        self.apply_dirty()
        self.layout()
        tr = self.media.track
        self.art.request(tr.art if (tr is not None and (P['art_colors'] or P['side_panel'])) else '')
        self.refresh_style()
        C, R = self.size

        if P['autocycle']:
            if self.next_cycle == 0:
                self.next_cycle = t0 + P['autocycle_s']
            elif t0 > self.next_cycle:
                self.step_mode(1, announce=False)
                self.next_cycle = t0 + P['autocycle_s']
        else:
            self.next_cycle = 0.0

        field = self.field
        x = self.audio.get()
        lv = self.spec.feed(x, P['sens'], P['attack'], P['release'], P['gate'],
                            P['agc'], P['agc_decay'])
        # idle breathing when nothing is playing
        if P['idle_anim']:
            self.silent = self.silent + dt if float(lv.max()) < .03 else 0.0
            if self.silent > 1.5:
                env = min(1.0, (self.silent - 1.5) / 1.5)
                ix_ = np.arange(field.nb) / field.nb
                fake = (.2 + .12 * np.sin(t0 * .9 + ix_ * 7) + .1 * np.sin(t0 * .37 - ix_ * 13)) * (1 - ix_ * .5)
                lv = np.maximum(lv, fake * env)
        self.lv = lv
        bass = float(lv[:max(2, field.nb // 12)].mean())
        self.bavg = self.bavg * .95 + bass * .05
        beat = bass > self.bavg * P['beat_thresh'] + .06 and t0 - self.blast > P['beat_cooldown']
        if beat:
            self.blast = t0
            self.flash = max(self.flash, min(1.0, .5 + bass))
        self.flash *= .86 ** (dt * 60)
        self.update_bg()
        span = P['morph']
        morph = 1.0 if span <= .01 else clamp((t0 - self.morph_start) / span, 0, 1)
        field.step(MODES.index(P['mode']), lv, bass, beat, self.mp, shocks, dt, t0,
                   self.spec.wave(x), P, morph)
        rows = field.render(self.lut, self.bg_now, self.effective_color_mode(), P['trail'],
                            P['symmetry'], P['sparkle'], t0)

        parts = ['\x1b[?2026h']
        self.hot = []
        for i, row in enumerate(rows):
            parts.append('\x1b[%d;%dH%s\x1b[0m' % (self.top + i + 1, self.fx0 + 1, row))
        if P['hud']:
            parts.append(self.hud(t0))
        if self.side_w:
            parts.append(self.draw_side(C, R))
        parts.append(self.draw_toast(t0))
        if self.overlay == 'settings':
            parts.append(self.draw_settings(C, R))
        elif self.overlay == 'help':
            parts.append(self.draw_help(C, R))
        parts.append('\x1b[?2026l')
        self.out.write(''.join(parts))
        self.out.flush()

    def run(self):
        def _stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _stop)
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _stop)
        try:
            self.term_on()
            if not self.cfg.meta.get('seen_help') and not self.args.no_intro:
                self.layout()
                self.open_overlay('help')
            if self.cfg.meta.get('version') != VERSION:
                self.cfg.meta['version'] = VERSION
                self.toast('vizr %s: press I for the info sidebar, L Z V for loop / shuffle / volume' % VERSION, 6.0)
            tprev = time.time()
            while self.running:
                t0 = time.time()
                dt = min(t0 - tprev, .1)
                tprev = t0
                self.frame(t0, dt)
                spent = time.time() - t0
                self.adapt_quality(spent)
                time.sleep(max(0, 1.0 / self.cfg['fps'] - spent))
        finally:
            self.term_off()
            self.audio.close()
            self.media.close()
            if self.cfg['autosave']:
                self.cfg.save()


# Command-line interface

def build_parser():
    ap = argparse.ArgumentParser(
        prog='vizr', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Terminal particle music visualizer with now-playing HUD.',
        epilog='Press ? inside the app for the full control list, S for the settings panel.\n'
               'Settings are stored in %s' % default_config_path())
    ap.add_argument('--version', action='version', version='vizr ' + VERSION)
    ap.add_argument('--demo', action='store_true', help='use the built-in demo signal instead of capturing audio')
    ap.add_argument('--device', default=None, help='sounddevice input device (name or index)')
    ap.add_argument('--monitor', default=None, help='PulseAudio/PipeWire monitor source for parec (default: default sink monitor)')
    ap.add_argument('--mode', default=None, help='start mode: 1-%d or one of %s' % (len(MODES), ', '.join(MODES)))
    ap.add_argument('--theme', default=None, help='start theme: ' + ', '.join(THEMES))
    ap.add_argument('--fps', type=int, default=None, help='target frame rate')
    ap.add_argument('--density', type=float, default=None, help='auto particle density multiplier')
    ap.add_argument('--count', type=int, default=None, help='fixed particle count (switches to manual count mode)')
    ap.add_argument('--transparent', action='store_true', help='do not paint a background colour')
    ap.add_argument('--no-mouse', action='store_true', help='disable mouse capture')
    ap.add_argument('--player', default=None, help='lock the HUD to this MPRIS player (e.g. spotify)')
    ap.add_argument('--prefer', default=None, help='comma list of players to rank first (default starts with spotify)')
    ap.add_argument('--list-players', action='store_true', help='print detected MPRIS players and which one wins, then exit')
    ap.add_argument('--config', default=None, help='path to the config file')
    ap.add_argument('--no-config', action='store_true', help='do not read or write a config file')
    ap.add_argument('--reset-config', action='store_true', help='ignore the saved config this run (it is overwritten on exit)')
    ap.add_argument('--no-intro', action='store_true', help='do not show the help screen on first launch')
    ap.add_argument('--credits', action='store_true', help='print project links and exit')
    return ap


def list_players(args):
    if not shutil.which('playerctl'):
        print('playerctl is not installed - install it to get now-playing info and media keys.')
        return 1
    prefer = [p.strip() for p in (args.prefer or '').split(',') if p.strip()]
    m = Media(prefer, args.player)
    players = m._fetch()
    m.close()
    if not players:
        print('No MPRIS players found. Start Spotify (or any player) and try again.')
        return 0
    best = m.choose(players)
    print('%-28s %-10s %-6s %s' % ('player (instance)', 'status', 'rank', 'track'))
    for p in sorted(players, key=m.rank):
        mark = '  <== shown in HUD' if p is best else ''
        print('%-28s %-10s %-6d %s - %s%s' % (p.inst or p.name, p.status, m.rank(p),
                                               p.artist, p.title, mark))
    return 0


def print_credits():
    print('vizr %s - terminal music visualizer' % VERSION)
    print('made by %s\n' % AUTHOR)
    print('  source    %s' % REPO)
    print('  package   %s' % PYPI)
    print('  github    %s' % GITHUB)
    print('  discord   %s' % DISCORD)
    print('\nThe same list lives in the settings panel under "Credits" (key 9).')


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.credits:
        print_credits()
        sys.exit(0)
    if args.list_players:
        sys.exit(list_players(args))
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        sys.exit('vizr needs an interactive terminal')
    App(args).run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
