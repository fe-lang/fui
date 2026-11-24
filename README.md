# tui

Terminal user interfaces for [Fe](https://github.com/argotorg/fe) programs:
layout, widgets, styled text, keyboard and mouse input, a diffing renderer and
an event loop, with a headless harness for tests.

It needs a Fe compiler built from the branch that adds `std::term` (raw mode,
terminal signals and the restore-on-panic record), which is not in Fe's `master`
yet. It runs on the native backend only. It is tested on Linux; the macOS code
paths in `std::term` are written but not verified, and there is no Windows
support. The library has no `unsafe` and no `extern`.

```toml
[dependencies]
tui = { source = "<repository URL>", rev = "<commit>" }
```

## A complete program

`examples/counter/src/lib.fe`:

```fe
use std::term::Tty
use std::time::Clock
use tui::prelude::*

/// The model: the count.
pub struct Counter { pub count: u64 }

impl Counter {
    // Sets the count, which is then drawn.
    fn set(mut self, _ count: u64) -> Flow {
        self.count = count
        Flow::Redraw
    }
}

impl App for Counter {
    uses E = ()

    fn update(mut self, _ event: Event) -> Flow uses E {
        match event {
            Event::Key(Key { code, mods, kind: KeyKind::Press }) => if mods.is_empty() {
                match code {
                    KeyCode::Up | KeyCode::Char(b'+') => self.set(self.count + 1),
                    KeyCode::Down | KeyCode::Char(b'-') => self.set(self.count.saturating_sub(1)),
                    KeyCode::Char(b'r') => self.set(0),
                    KeyCode::Char(b'q') | KeyCode::Esc => Flow::Quit,
                    _ => Flow::Unchanged,
                }
            } else {
                Flow::Unchanged
            },
            _ => Flow::Unchanged,
        }
    }

    fn view(mut self, _ area: Rect) uses (frame: mut Frame) {
        let (head, body, foot) = area.rows((Length(1), Fill(1), Length(1)))
        let theme = theme()
        print(head.pos(), "Counter", theme.title)
        let text = format!("Count: ", self.count)
        // The text is ASCII: one column a byte.
        let line = body.centered(width: text.len().downcast_truncate(), height: 1)
        print(line.pos(), text, theme.accent)
        print(
            foot.pos(),
            "Up/+ add  Down/- subtract  r reset  q quit",
            theme.muted,
        )
    }
}

pub fn main() -> i32 uses (tty: mut Tty, clock: Clock) {
    var counter = Counter { count: 0 }
    match run(mut counter, Options::new()) {
        Result::Ok(()) => 0,
        Result::Err(_) => 1,
    }
}
```

Build it with `fe build --backend native --out-dir out examples/counter`.

## The shape of the library

| Part | How |
| --- | --- |
| Configuration | By value: a widget is a small `Copy` value built by methods, `Block::bordered().rounded()` |
| Data | By view: a render takes the items, rows or text as a parameter, nothing is copied |
| State | By `mut`: selection, scroll and cursor live in the model, `List::new().render(area, items, mut list)` |
| Drawing | Through the `Frame` effect: view code declares `uses (frame: mut Frame)` |
| Capabilities | In the app's effect row: `impl App for Browser { uses E = (fs: Fs) .. }`, provided by `main` |
| `view` | Has the frame as its only effect, so it cannot do I/O |
| Scoped changes | Projections: `with (Frame = clipped(area)) { .. }`, `with (Frame = themed(theme)) { .. }` |
| Restoring | `run` gives the terminal back on every return; a panic restores it before its report |

## Layout

A `Rect` splits by a tuple of constraints into a tuple of rectangles
(`Length`, `Min`, `Max`, `Percent`, `Ratio`, `Fill`). From `examples/gallery`:

```fe
let (head, body, foot) = area.rows((Length(1), Fill(1), Length(1)))
let (left, right) = area.columns((Length(15), Fill(1)))
let (name, email, password, _, echo) = inner
    .rows((Length(1), Length(1), Length(1), Length(1), Fill(1)))
```

`Layout::rows()` and `Layout::columns()` add spacing, margins and a `Flex`;
`Rect` has `split_top`, `split_left`, `centered` and `shrink` for one-off cuts.

## Widgets

- `Block`: borders (plain, rounded, double, thick, ASCII), titles, padding; `render` returns the inner area.
- `Paragraph`: text, a styled `Line` or `Lines`, wrapped by word or cluster, aligned, scrolled.
- `List` and `ListState`: items with a selection; `render_with` draws each row with a closure.
- `Table` and `TableState`: titled columns with solved widths and alignment; `render_with` draws each cell.
- `Tabs` and `TabsState`: a row of titles, scrolled to keep the selected one shown.
- `TextInput` and `TextInputState`: a one-line field with a cursor, a selection, word motions and a mask.
- `Gauge` and `LineGauge`: progress as a filled bar or a line.
- `Sparkline`: a bar chart of recent values in eighths of a cell.
- `Spinner`: an animation driven by a tick counter.
- `Scrollbar`: a track and thumb for any `ScrollState`.
- `clear` and `popup`: what is drawn over other widgets.

A list beside a scrolled paragraph with a scrollbar (lines from `examples/gallery`):

```fe
List::new().highlight_symbol("> ").render(names, items.items, mut items.list)

Option::Some(i) => Paragraph::new()
    .wrap(WrapMode::Word)
    .render_scrolled(detail, items.items[i].about, mut items.detail),

Scrollbar::vertical().render_for(track, items.detail)
```

## Events

`Event` is `Key`, `Mouse`, `Paste`, `Resize`, `FocusGained`, `FocusLost`,
`Tick`, `Terminate`, `Resume` or `Reply`. A key is a `KeyCode`, `Modifiers` and
a `KeyKind` (press, repeat, release), matched by pattern as in the counter, or
tested with `key.is(KeyCode::Esc)`, `key.is_char(b'q')`, `key.ctrl(b'c')`.
Widget states take events themselves: `handle_key` returns whether the key was
theirs, `handle_mouse` what a click hit. An `update` that changes a list's
items calls `ListState::set_len`, so keys queued before the next render move
within the new items. The parser reads the CSI and SS3 key sequences, the kitty
keyboard protocol, xterm's `modifyOtherKeys`, SGR and X10 mouse reports,
bracketed paste and focus reports.

## Styles and themes

A `Style` is a foreground, a background and attributes; `patch` lays one over
another. Widgets take their styles from the frame's `Theme` by role (`text`,
`muted`, `border`, `title`, `highlight`, `accent`, `error`, ..), so switching
the theme restyles everything. The gallery's `view` switches it with a scoped
projection (or `Options::theme` sets it for the whole run):

```fe
    with (Frame = themed(theme)) {
        self.draw(area)
    }
```

Styled text is a `Line` of runs, or `Lines` of them, drawn by `Paragraph` or
`print_line`.

## The event loop

`run(mut app, options)` takes the terminal over, draws the first frame, and
runs `update` and `view` until `Flow::Quit`, redrawing on `Flow::Redraw`, a
resize or a resume once queued events are handled. Ctrl+C quits and Ctrl+Z
suspends unless the options give them to `update`. `Options` also sets mouse
tracking, focus events, kitty keyboard flags, color depth, theme, Esc timeout
and ticks:

```fe
pub fn options() -> Options {
    Options::new().mouse(MouseTracking::Clicks).tick(Duration::from_millis(100))
}
```

An app whose `update` needs a capability names it in its row, and `main`
provides it with `run`'s own (lines from `examples/files`):

```fe
impl App for Browser {
    uses E = (fs: Fs)

pub fn main() -> i32 uses (tty: mut Tty, clock: Clock, fs: Fs, args: Args) {
```

`view` cannot read the file system: `App::view` declares the frame as its only
effect. Using `fs` in its body, or adding it to its row, is rejected:

```text
error[8-0012]: undefined variable
252 │         self.preview = fs.read(self.dir).unwrap()
    │                        ^^ undefined variable `fs`

error[6-0024]: method `view` has incompatible effects
243 │     fn view(mut self, _ area: Rect) uses (frame: mut Frame, fs: Fs) {
    │        ^^^^ expected 1 effects, found 2
```

## Running your own loop

A program that waits on more than the terminal drives a `Session` itself.
`examples/listen` registers the session's `input_fd` and `signal_fd` with a
`std::net::Poller` beside a TCP listener, and in its loop draws, waits, pumps
the session and takes its events with `pop_event` (lines from its loop):

```fe
poller.add(Source::Terminal(session.input_fd()), Interest::READABLE)?
poller.add(Source::Terminal(session.signal_fd()), Interest::READABLE)?
let listening = poller.add(Source::Listener(listener), Interest::READABLE)?

    with (Frame = session.frame()) {
        server.view(area)
    }
    session.present()?

    poller.wait(mut events, session.pending_timeout())?

    session.pump()?
```

## Testing with `Harness`

`Harness` runs an `App` over a scripted terminal, the real renderer feeding a
terminal emulator. A test sends events (`key`, `type_text`, `click`, `mouse`,
`paste`, `tick`, `resize`, or raw bytes with `input`) and reads the screen
(`screen`, `cell`) or the model (`app`), as in `examples/counter`:

```fe
#[test]
fn the_count_stays_centered_when_the_terminal_resizes() {
    var harness = Harness::new(Counter { count: 7 }, Size::new(width: 24, height: 5))
    harness.resize(Size::new(width: 12, height: 3))
    assert(harness.screen() == "Counter\n  Count: 7\nUp/+ add  Do")
}
```

## What is checked

- 291 library tests; the examples add 22 (`counter` 2, `events` 3, `gallery` 8,
  `files` 8, `listen` 1). The `files` tests build a directory tree through `Fs`.
- All 1093 cases of Unicode 16.0.0 `GraphemeBreakTest.txt` for cluster breaks.
- The renderer's property test: 6000 random buffers of random sizes, contents
  and color depths, each drawn whole and then as a diff from the one before,
  fed to the terminal emulator, which must show exactly the buffer.
- `dev/pty_check.py` runs `counter` on a pseudo-terminal: entry, keys, resize,
  Ctrl+Z and SIGTSTP and SIGCONT, SIGTERM, Ctrl+C, `q` and hangup, each exit
  with the modes and terminal attributes as before the start.
- `dev/pty_gallery.py` runs `gallery` (keys, SGR clicks on rows and tabs,
  resize, quit), `panic` (a panic restores the terminal before its report,
  which is visible on the main screen) and `listen` (TCP connections listed).

## Limitations

- `TextInput` is one line; there is no multi-line editor.
- Full screen only: there is no inline mode that draws below the prompt
  without the alternate screen.
- No images (sixel, kitty graphics).
- Widths follow Unicode's tables, emoji sequences and flags 2 columns. Terminals
  disagree on some clusters; the renderer places the cell after one with an
  absolute move, so nothing else shifts, but the cluster itself may look cut.
- macOS is unverified and Windows is not supported.

Fe shapes some of the API:

- Fe has no `char`, so `KeyCode::Char(u8)` holds printable ASCII and
  `KeyCode::Unicode(u32)` every other character: `KeyCode::Char(b'q')`.
- There are no array patterns, so splits destructure tuples:
  `let (a, b) = area.rows((Length(1), Fill(1)))`.
- `List::render_with` and `Table::render_with` clip each row's closure to it.
  User code cannot give its own closures a scoped frame: `with (Frame =
  clipped(..))` around a call of a closure with a forwarded effect row does not
  reach it, and the methods the widgets use instead are internal.
- Closures capture by copy, so row closures get their data as the row argument.

## Running the checks

```sh
fe test --backend native .
for e in counter events gallery files listen; do fe test --backend native examples/$e; done
for e in counter gallery panic listen; do fe build --backend native --out-dir out examples/$e; done
python3 -B dev/pty_check.py out/counter
python3 -B dev/pty_gallery.py out/gallery out/panic out/listen
```

## License

Licensed under the [Apache License, Version 2.0](./LICENSE-APACHE).
