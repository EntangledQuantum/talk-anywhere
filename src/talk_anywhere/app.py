"""Tray app and settings window. The microphone stays closed until dictation is on."""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, Gdk, GLib, Gtk  # noqa: E402

from talk_anywhere.committer import DictationCommitter
from talk_anywhere.config import Config
from talk_anywhere.icons import ensure_icons
from talk_anywhere.inject import InjectError, assert_foreign_focus, backspace, type_text
from talk_anywhere.install_desktop import install_autostart
from talk_anywhere.mics import MicError, list_mics, resolve_mic
from talk_anywhere.models import directory, get_model, model_ready, processor_source
from talk_anywhere.paths import ENGINE_PYTHON, ROOT, log_path
from talk_anywhere.sounds import PAIRS, ensure_sounds, play
from talk_anywhere.shortcut import (
    accelerator_label,
    binding_from_event,
    pretty_shortcut,
    register_shortcut,
    validate_shortcut,
)
from talk_anywhere.single_instance import CommandServer

log = logging.getLogger("talkanywhere")


class Injector:
    """Applies transcript edits in order, on one thread, so partials cannot overlap."""

    def __init__(self, on_error) -> None:
        self.on_error = on_error
        self.queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.committer = DictationCommitter()
        self.enabled = False
        self.failed = False
        threading.Thread(target=self._run, name="injector", daemon=True).start()

    def submit(self, text: str, final: bool) -> None:
        self.queue.put(("text", text, final))

    def submit_reset(self) -> None:
        self.queue.put(("reset", "", False))

    def _run(self) -> None:
        while True:
            kind, text, final = self.queue.get()
            if kind == "reset":
                self._reset()
                continue
            if not final:
                skipped = False
                while True:
                    try:
                        nxt_kind, nxt_text, nxt_final = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt_kind == "reset":
                        self._apply(text, False)
                        self._reset()
                        skipped = True
                        break
                    if nxt_final:
                        self._apply(text, False)
                        self._apply(nxt_text, True)
                        skipped = True
                        break
                    text = nxt_text
                if skipped:
                    continue
            self._apply(text, final)

    def _reset(self) -> None:
        with self.lock:
            self.committer.reset()
            self.failed = False

    def _apply(self, text: str, final: bool) -> None:
        with self.lock:
            if self.failed or (not self.enabled and not final):
                return
            edits = self.committer.update(text, final=final)
        if not edits:
            return
        try:
            assert_foreign_focus()
        except InjectError as exc:
            self._fail(str(exc))
            return
        try:
            for kind, value in edits:
                if kind == "backspace":
                    backspace(int(value))
                else:
                    type_text(str(value))
        except InjectError as exc:
            self._fail(str(exc))

    def _fail(self, message: str) -> None:
        with self.lock:
            if self.failed:
                return
            self.failed = True
            self.committer.reset()
        self.on_error(message)


class TalkApp:
    def __init__(self) -> None:
        self.config = Config.load()
        self.icons = ensure_icons()
        self.proc: subprocess.Popen | None = None
        self.model_loaded = False
        self.gpu_name = ""
        self.listening = False
        self.accept_text = False
        self.pending_toggle = os.environ.get("TALK_ANYWHERE_ON_READY") == "toggle"
        self.quitting = False
        self.capturing = False
        self.loading_mics = False
        self.loading_models = False
        self.loading_sounds = False
        self.downloading = False
        self.state = "starting"
        self.error = ""
        self.stderr_tail = ""
        self.server: CommandServer | None = None
        self.injector = Injector(self._inject_error_from_thread)
        self._build_window()
        self._build_indicator()
        self._reload_mics()
        self._update_visuals()
        ensure_sounds()
        spec = get_model(self.config.model_key)
        if model_ready(spec):
            self._spawn_engine()
        else:
            self.state = "needs-model"
            self._update_visuals()
        if not self.config.intro_shown or not model_ready(spec):
            self.show()

    def _build_window(self) -> None:
        self.window = Gtk.Window(title="Talk Anywhere")
        self.window.set_default_size(520, 680)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.set_icon_from_file(str(self.icons["app.png"]))
        self.window.set_wmclass("talk-anywhere", "Talk Anywhere")
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Talk Anywhere")
        header.set_subtitle("English dictation")
        self.window.set_titlebar(header)

        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            .hint { color: #5e564c; font-size: 13px; }
            .credit { color: #7a7168; font-size: 12px; }
            .error-banner { background: #f8e4df; color: #6e221c; padding: 10px; border-radius: 8px; }
            .status-ready { color: #1d4a28; }
            .status-live { color: #8a5a00; font-weight: 600; }
            .status-bad { color: #6e221c; }
            """
        )
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=18)
        self.window.add(grid)

        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_line_wrap(True)
        grid.attach(self.status_label, 0, 0, 3, 1)

        self.error_label = Gtk.Label(xalign=0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_max_width_chars(46)
        self.error_label.get_style_context().add_class("error-banner")
        self.error_box = Gtk.Box()
        self.error_box.add(self.error_label)
        self.error_box.set_no_show_all(True)
        grid.attach(self.error_box, 0, 1, 3, 1)

        grid.attach(self._label("Microphone"), 0, 2, 1, 1)
        self.mic_store = Gtk.ListStore(str, str, str)
        self.mic_combo = Gtk.ComboBox.new_with_model(self.mic_store)
        cell = Gtk.CellRendererText()
        self.mic_combo.pack_start(cell, True)
        self.mic_combo.add_attribute(cell, "text", 0)
        self.mic_combo.connect("changed", self._on_mic_changed)
        grid.attach(self.mic_combo, 1, 2, 1, 1)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self._reload_mics())
        grid.attach(refresh, 2, 2, 1, 1)

        self.mic_note = Gtk.Label(xalign=0)
        self.mic_note.set_line_wrap(True)
        self.mic_note.get_style_context().add_class("hint")
        grid.attach(self.mic_note, 0, 3, 3, 1)

        grid.attach(self._label("Shortcut"), 0, 4, 1, 1)
        self.shortcut_value = Gtk.Label(xalign=0)
        grid.attach(self.shortcut_value, 1, 4, 1, 1)
        self.shortcut_button = Gtk.Button(label="Change")
        self.shortcut_button.connect("clicked", self._begin_capture)
        grid.attach(self.shortcut_button, 2, 4, 1, 1)

        self.shortcut_hint = Gtk.Label(xalign=0)
        self.shortcut_hint.set_line_wrap(True)
        self.shortcut_hint.get_style_context().add_class("hint")
        grid.attach(self.shortcut_hint, 0, 5, 3, 1)

        grid.attach(self._label("Model"), 0, 6, 1, 1)
        self.model_store = Gtk.ListStore(str, str)
        self.model_combo = Gtk.ComboBox.new_with_model(self.model_store)
        model_cell = Gtk.CellRendererText()
        self.model_combo.pack_start(model_cell, True)
        self.model_combo.add_attribute(model_cell, "text", 0)
        self._fill_models()
        self.model_combo.connect("changed", self._on_model_changed)
        grid.attach(self.model_combo, 1, 6, 2, 1)

        self.model_label = Gtk.Label(xalign=0)
        self.model_label.set_line_wrap(True)
        self.model_label.get_style_context().add_class("hint")
        grid.attach(self.model_label, 1, 7, 2, 1)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_no_show_all(True)
        grid.attach(self.progress, 0, 8, 3, 1)

        self.download_button = Gtk.Button(label="Download model")
        self.download_button.get_style_context().add_class("suggested-action")
        self.download_button.connect("clicked", lambda _b: self._start_download())
        self.download_button.set_no_show_all(True)
        grid.attach(self.download_button, 0, 9, 3, 1)

        grid.attach(self._label("Sounds"), 0, 10, 1, 1)
        self.sound_store = Gtk.ListStore(str, str)
        self.sound_combo = Gtk.ComboBox.new_with_model(self.sound_store)
        sound_cell = Gtk.CellRendererText()
        self.sound_combo.pack_start(sound_cell, True)
        self.sound_combo.add_attribute(sound_cell, "text", 0)
        self._fill_sounds()
        self.sound_combo.connect("changed", self._on_sound_changed)
        grid.attach(self.sound_combo, 1, 10, 1, 1)
        preview = Gtk.Button(label="Preview")
        preview.connect("clicked", lambda _b: self._preview_sounds())
        grid.attach(preview, 2, 10, 1, 1)

        self.login_check = Gtk.CheckButton(label="Start in the tray when I log in")
        self.login_check.set_active(self.config.start_on_login)
        self.login_check.connect("toggled", self._on_login_toggled)
        grid.attach(self.login_check, 0, 11, 3, 1)

        self.action_button = Gtk.Button(label="Start dictation")
        self.action_button.connect("clicked", lambda _b: self.toggle())
        grid.attach(self.action_button, 0, 12, 3, 1)

        self.hint = Gtk.Label(xalign=0)
        self.hint.set_line_wrap(True)
        self.hint.get_style_context().add_class("hint")
        grid.attach(self.hint, 0, 13, 3, 1)

        credit = Gtk.Label(
            label="English only. Bark models are by Conway Research, based on NVIDIA Parakeet.",
            xalign=0,
        )
        credit.set_line_wrap(True)
        credit.get_style_context().add_class("credit")
        grid.attach(credit, 0, 14, 3, 1)

        self.gpu_label = Gtk.Label(xalign=0)
        self.gpu_label.get_style_context().add_class("credit")
        grid.attach(self.gpu_label, 0, 15, 3, 1)

        self.window.connect("delete-event", self._on_close)
        self.window.connect("focus-out-event", self._on_focus_out)
        self.window.connect("key-press-event", self._on_key)

    def _label(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        return label

    def _build_indicator(self) -> None:
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "talk-anywhere",
            "audio-input-microphone",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_full(str(self.icons["idle.png"]), "Talk Anywhere")
        self.indicator.set_attention_icon_full(str(self.icons["error.png"]), "Talk Anywhere error")
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Talk Anywhere")

        menu = Gtk.Menu()
        self.menu_status = Gtk.MenuItem(label="Starting")
        self.menu_status.set_sensitive(False)
        menu.append(self.menu_status)
        menu.append(Gtk.SeparatorMenuItem())
        self.menu_toggle = Gtk.MenuItem(label="Start dictation")
        self.menu_toggle.connect("activate", lambda _i: self.toggle())
        menu.append(self.menu_toggle)
        settings = Gtk.MenuItem(label="Settings")
        settings.connect("activate", lambda _i: self.show())
        menu.append(settings)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _i: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def show(self) -> None:
        self._reload_mics()
        self.window.show_all()
        self._update_visuals()
        self.window.present()

    def _on_close(self, *_args) -> bool:
        if model_ready(get_model(self.config.model_key)) and self.config.shortcut:
            self.config.intro_shown = True
            self.config.save()
        self.capturing = False
        self.window.hide()
        return True

    def _begin_capture(self, *_args) -> None:
        self.capturing = True
        self.shortcut_hint.set_text(
            "Press the Windows key plus another key. Esc cancels. Leaving this window cancels it too."
        )
        self.shortcut_button.set_label("Listening for keys")

    def _on_focus_out(self, *_args) -> bool:
        # The global shortcut keeps working. Only the "press a new shortcut" capture stops.
        if self.capturing:
            self.capturing = False
            self.shortcut_hint.set_text("")
            self._update_visuals()
        return False

    def _on_key(self, _widget, event) -> bool:
        if not self.capturing:
            return False
        binding = binding_from_event(event)
        if binding is None:
            return True
        if binding == "cancel":
            self.capturing = False
            self.shortcut_hint.set_text("")
            self._update_visuals()
            return True
        problem = validate_shortcut(binding)
        if problem:
            self.shortcut_hint.set_text(problem)
            return True
        self.capturing = False
        self.shortcut_hint.set_text("")
        try:
            register_shortcut(binding, f"{_launcher()} toggle")
        except Exception as exc:
            self.set_error(f"Could not save the shortcut. {exc}")
            return True
        self.config.shortcut = binding
        self.config.save()
        self._update_visuals()
        self._notify("Shortcut saved", pretty_shortcut(binding) + " toggles dictation.")
        return True

    def _on_login_toggled(self, button) -> None:
        self.config.start_on_login = bool(button.get_active())
        self.config.save()
        install_autostart(self.config.start_on_login, str(self.icons["app.png"]))

    def _reload_mics(self) -> None:
        self.loading_mics = True
        self.mic_store.clear()
        try:
            mics = list_mics()
        except MicError as exc:
            self.mic_note.set_text(str(exc))
            self.loading_mics = False
            return
        for mic in mics:
            self.mic_store.append((mic.label, mic.name, mic.description))
        from talk_anywhere.mics import prefer_camera

        chosen = resolve_mic(mics, self.config.mic_name, self.config.mic_description)
        if chosen is None and not self.config.mic_name:
            chosen = prefer_camera(mics)
        if chosen is None:
            self.mic_combo.set_active(-1)
            names = ", ".join(mic.description for mic in mics) or "none"
            wanted = self.config.mic_description or "The saved microphone"
            self.mic_note.set_text(
                f"{wanted} is not available, so no microphone is selected. "
                f"Found: {names}. The system default input was not changed."
            )
        else:
            if chosen.name != self.config.mic_name:
                self.config.mic_name = chosen.name
                self.config.mic_description = chosen.description
                self.config.save()
            for index, row in enumerate(self.mic_store):
                if row[1] == chosen.name:
                    self.mic_combo.set_active(index)
                    break
            self.mic_note.set_text(
                f"Using {chosen.description} only while dictation is on. "
                "The system default microphone is left as it is."
            )
        self.loading_mics = False

    def _on_mic_changed(self, combo) -> None:
        if self.loading_mics:
            return
        active = combo.get_active_iter()
        if active is None:
            return
        name = self.mic_store.get_value(active, 1)
        description = self.mic_store.get_value(active, 2)
        if name == self.config.mic_name:
            return
        self.config.mic_name = name
        self.config.mic_description = description
        self.config.save()
        self.mic_note.set_text(
            f"Using {description} only while dictation is on. "
            "The system default microphone is left as it is."
        )
        if self.listening:
            self._send({"cmd": "stop"})
            self.set_error("Microphone changed. Turn dictation on again to use it.")

    def _start_download(self) -> None:
        if self.downloading:
            return
        if not ENGINE_PYTHON.is_file():
            self.set_error(f"The speech engine is not installed at {ENGINE_PYTHON}.")
            return
        self.downloading = True
        self.download_button.set_sensitive(False)
        self.progress.show()
        self.progress.set_fraction(0)
        self.progress.set_text("Starting download")
        threading.Thread(target=self._download_thread, daemon=True).start()

    def _download_thread(self) -> None:
        try:
            proc = subprocess.Popen(
                [str(ENGINE_PYTHON), "-u", "-m", "talk_anywhere.download", self.config.model_key],
                cwd=str(ROOT),
                env=_engine_env(offline=False),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            GLib.idle_add(self._download_finished, f"Could not start the download. {exc}")
            return
        assert proc.stdout is not None
        error = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.info("download: %s", line)
                continue
            if event.get("event") == "progress":
                GLib.idle_add(self._download_progress, event.get("bytes") or 0, event.get("total") or 0)
            elif event.get("event") == "error":
                error = event.get("message") or "Download failed."
            elif event.get("event") == "done":
                error = ""
        code = proc.wait()
        if code != 0 and not error:
            error = f"Download exited with status {code}."
        GLib.idle_add(self._download_finished, error)

    def _download_progress(self, done: int, total: int) -> bool:
        if total <= 0:
            self.progress.pulse()
            self.progress.set_text(f"Downloading {get_model(self.config.model_key).title}")
        else:
            fraction = max(0.0, min(1.0, done / total))
            self.progress.set_fraction(fraction)
            self.progress.set_text(f"{fraction * 100:.0f}%")
        return False

    def _download_finished(self, error: str) -> bool:
        self.downloading = False
        self.download_button.set_sensitive(True)
        if error:
            self.set_error(error)
            return False
        if not model_ready(get_model(self.config.model_key)):
            title = get_model(self.config.model_key).title
            self.set_error(f"{title} is still incomplete after the download.")
            return False
        self.clear_error()
        self.state = "loading"
        self._update_visuals()
        self._spawn_engine()
        return False

    def _load_command(self) -> dict:
        spec = get_model(self.config.model_key)
        return {
            "cmd": "load",
            "model_dir": str(directory(spec)),
            "kind": spec.kind,
            "processor_dir": str(processor_source(spec)),
            "title": spec.title,
        }

    def _spawn_engine(self) -> None:
        spec = get_model(self.config.model_key)
        if not model_ready(spec):
            self.state = "needs-model"
            self._update_visuals()
            return
        if self.proc is not None and self.proc.poll() is None:
            self.model_loaded = False
            self.state = "loading"
            self._update_visuals()
            self._send(self._load_command())
            return
        if not ENGINE_PYTHON.is_file():
            self.set_error(f"The speech engine is not installed at {ENGINE_PYTHON}.")
            return
        self.state = "loading"
        self.model_loaded = False
        self._update_visuals()
        try:
            self.proc = subprocess.Popen(
                [str(ENGINE_PYTHON), "-u", "-m", "talk_anywhere.engine"],
                cwd=str(ROOT),
                env=_engine_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.set_error(f"Could not start the speech engine. {exc}")
            return
        proc = self.proc
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        threading.Thread(target=self._wait_engine, args=(proc,), daemon=True).start()
        self._send(self._load_command())

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("engine said: %s", line)
                continue
            GLib.idle_add(self._on_engine_event, event)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            text = line.rstrip()
            if text:
                log.info("engine: %s", text)
                self.stderr_tail = (self.stderr_tail + "\n" + text)[-2000:]

    def _wait_engine(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        GLib.idle_add(self._engine_exited, proc, code)

    def _engine_exited(self, proc: subprocess.Popen, code: int) -> bool:
        if self.proc is not proc:
            return False
        self.proc = None
        self.model_loaded = False
        self.listening = False
        self.accept_text = False
        if self.quitting:
            return False
        tail = self.stderr_tail.strip().splitlines()
        detail = tail[-1] if tail else ""
        self.set_error(
            f"The speech engine stopped (exit {code}). {detail}".strip()
        )
        return False

    def _on_engine_event(self, event: dict) -> bool:
        name = event.get("event")
        if name == "ready":
            self.model_loaded = True
            self.gpu_name = str(event.get("gpu") or "")
            if self.state != "error":
                self.clear_error()
            self.state = "ready"
            self._update_visuals()
            if self.pending_toggle:
                self.pending_toggle = False
                self._request_start()
        elif name == "listening":
            self.listening = True
            self.accept_text = True
            self.injector.enabled = True
            self.injector.submit_reset()
            self.clear_error()
            self.state = "listening"
            self._update_visuals()
            play(self.config.sound_pair, "start")
            self._notify(
                "Listening",
                f"{self.config.mic_description}. Press {pretty_shortcut(self.config.shortcut)} to stop.",
            )
        elif name == "partial" and self.accept_text:
            self.injector.submit(str(event.get("text") or ""), False)
        elif name == "final" and self.accept_text:
            self.injector.submit(str(event.get("text") or ""), True)
        elif name == "idle":
            was = self.listening
            self.listening = False
            self.accept_text = False
            self.injector.enabled = False
            self.injector.submit_reset()
            if self.state != "error":
                self.state = "ready" if self.model_loaded else self.state
            self._update_visuals()
            if was:
                play(self.config.sound_pair, "stop")
                self._notify("Dictation off", "The microphone is closed.")
        elif name == "error":
            message = str(event.get("message") or "Something went wrong.")
            was_listening = self.listening
            # Release the microphone if a capture stream is still up.
            if self.listening or self.state == "opening-mic":
                self._send({"cmd": "stop"})
            self.listening = False
            if was_listening:
                play(self.config.sound_pair, "stop")
            self.accept_text = False
            self.injector.enabled = False
            self.set_error(message)
            self._notify("Talk Anywhere", message)
        elif name == "status" and event.get("state") == "loading":
            self.state = "loading"
            self._update_visuals()
        return False

    def _send(self, payload: dict) -> bool:
        proc = self.proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            self.set_error("The speech engine is not running.")
            return False
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.set_error(f"Could not talk to the speech engine. {exc}")
            return False
        return True

    def toggle(self) -> None:
        if self.listening or self.state == "opening-mic":
            self._send({"cmd": "stop"})
            self.state = "stopping"
            self._update_visuals()
            return
        if not model_ready(get_model(self.config.model_key)):
            self.state = "needs-model"
            self._update_visuals()
            self.show()
            return
        if not self.config.shortcut:
            self.show()
            self.set_error("Set a Windows-key shortcut first.")
            return
        if not self.config.mic_name:
            self.show()
            self.set_error("Choose a microphone first.")
            return
        if self.proc is None or self.proc.poll() is not None or not self.model_loaded:
            self.pending_toggle = True
            self._spawn_engine()
            self.show()
            return
        self._request_start()

    def _request_start(self) -> None:
        self.state = "opening-mic"
        self._update_visuals()
        if not self._send({"cmd": "start", "device": self.config.mic_name}):
            self.pending_toggle = False

    def set_error(self, message: str) -> None:
        self.error = message
        self.state = "error"
        log.error("%s", message)
        self._update_visuals()
        if not self.window.get_visible():
            self.show()

    def clear_error(self) -> None:
        self.error = ""

    def _inject_error_from_thread(self, message: str) -> None:
        GLib.idle_add(self._inject_error, message)

    def _inject_error(self, message: str) -> bool:
        if self.listening or self.accept_text:
            self._send({"cmd": "stop"})
        self.accept_text = False
        self.injector.enabled = False
        self.set_error(message)
        self._notify("Dictation stopped", message)
        return False

    def _fill_models(self) -> None:
        from talk_anywhere.models import MODELS

        self.loading_models = True
        self.model_store.clear()
        active = 0
        for index, spec in enumerate(MODELS):
            self.model_store.append((spec.label, spec.key))
            if spec.key == self.config.model_key:
                active = index
        self.model_combo.set_active(active)
        self.loading_models = False

    def _on_model_changed(self, combo) -> None:
        if self.loading_models:
            return
        active = combo.get_active_iter()
        if active is None:
            return
        key = self.model_store.get_value(active, 1)
        if key == self.config.model_key:
            return
        self.config.model_key = key
        self.config.save()
        self.model_loaded = False
        self._update_visuals()
        if model_ready(get_model(key)):
            self._spawn_engine()

    def _fill_sounds(self) -> None:
        self.loading_sounds = True
        self.sound_store.clear()
        active = 0
        for index, (key, spec) in enumerate(PAIRS.items()):
            self.sound_store.append((spec["title"], key))
            if key == self.config.sound_pair:
                active = index
        self.sound_combo.set_active(active)
        self.loading_sounds = False

    def _on_sound_changed(self, combo) -> None:
        if self.loading_sounds:
            return
        active = combo.get_active_iter()
        if active is None:
            return
        key = self.sound_store.get_value(active, 1)
        if key == self.config.sound_pair:
            return
        self.config.sound_pair = key
        self.config.save()

    def _preview_sounds(self) -> None:
        play(self.config.sound_pair, "start")
        GLib.timeout_add(420, self._preview_stop)

    def _preview_stop(self) -> bool:
        play(self.config.sound_pair, "stop")
        return False

    def _update_visuals(self) -> None:
        shortcut = pretty_shortcut(self.config.shortcut)
        spec = get_model(self.config.model_key)
        self.shortcut_value.set_text(shortcut)
        if not self.capturing:
            self.shortcut_button.set_label("Change")
        ready = model_ready(spec)
        if ready:
            self.model_label.set_text(f"{spec.title} is downloaded. {spec.summary}.")
            self.download_button.hide()
            self.progress.hide()
        else:
            self.model_label.set_text(f"{spec.title} is not downloaded yet. {spec.summary}.")
            self.download_button.set_label(f"Download {spec.title}")
            self.download_button.show()
            if self.downloading:
                self.progress.show()
        self.gpu_label.set_text(self.gpu_name or "GPU not loaded yet")
        self.hint.set_text(
            f"Click a text field, then press {shortcut}. "
            "Words appear while you speak. Press the same shortcut again to stop. "
            "The microphone opens only while dictation is on. A sound marks the start and the stop."
        )
        if self.error:
            self.error_label.set_text(self.error)
            self.error_box.show()
            self.error_label.show()
        else:
            self.error_box.hide()

        if self.state == "listening":
            status = f"Listening on {self.config.mic_description}."
            style = "status-live"
            icon = self.icons["listening.png"]
            attention = False
            action = "Stop dictation"
            menu = f"Stop dictation  ({shortcut})"
        elif self.state == "loading":
            status = f"Loading {get_model(self.config.model_key).title} onto the GPU…"
            style = "hint"
            icon = self.icons["idle.png"]
            attention = False
            action = "Loading…"
            menu = "Loading model"
        elif self.state == "opening-mic" or self.state == "stopping":
            status = "Switching the microphone…"
            style = "hint"
            icon = self.icons["idle.png"]
            attention = False
            action = "Stop dictation" if self.state == "opening-mic" else "Start dictation"
            menu = action
        elif self.state == "needs-model":
            status = f"Download {get_model(self.config.model_key).title} to begin."
            style = "hint"
            icon = self.icons["idle.png"]
            attention = False
            action = "Download required"
            menu = "Download the model first"
        elif self.state == "error":
            status = "Something needs attention."
            style = "status-bad"
            icon = self.icons["error.png"]
            attention = True
            action = "Try again"
            menu = "Try again"
        else:
            status = f"Ready. {shortcut} toggles dictation."
            style = "status-ready"
            icon = self.icons["idle.png"]
            attention = False
            action = "Start dictation"
            menu = f"Start dictation  ({shortcut})"

        self.status_label.set_text(status)
        context = self.status_label.get_style_context()
        for name in ("status-ready", "status-live", "status-bad", "hint"):
            context.remove_class(name)
        context.add_class(style)
        self.action_button.set_label(action)
        self.action_button.set_sensitive(self.state not in ("loading", "needs-model"))
        self.menu_status.set_label(_short(self.error or status))
        self.menu_toggle.set_label(menu)
        self.indicator.set_icon_full(str(icon), "Talk Anywhere")
        self.indicator.set_title("Talk Anywhere — " + _short(self.error or status, 120))
        self.indicator.set_status(
            AyatanaAppIndicator3.IndicatorStatus.ATTENTION
            if attention
            else AyatanaAppIndicator3.IndicatorStatus.ACTIVE
        )

    def _notify(self, summary: str, body: str) -> None:
        try:
            subprocess.Popen(
                [
                    "notify-send",
                    "-a",
                    "Talk Anywhere",
                    "-i",
                    str(self.icons["app.png"]),
                    summary,
                    body,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.info("notify failed: %s %s", summary, body)

    def on_command(self, command: str) -> str:
        if command == "toggle":
            GLib.idle_add(self.toggle)
            return "ok"
        if command == "show":
            GLib.idle_add(self.show)
            return "ok"
        if command == "quit":
            GLib.idle_add(self.quit)
            return "ok"
        if command == "status":
            return self.state
        return "unknown"

    def quit(self, *_args) -> bool:
        if self.quitting:
            return False
        self.quitting = True
        self.accept_text = False
        try:
            if self.proc is not None and self.proc.poll() is None and self.proc.stdin is not None:
                self.proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                self.proc.stdin.flush()
        except OSError:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        if self.server is not None:
            self.server.close()
        Gtk.main_quit()
        return False


def _launcher() -> str:
    from talk_anywhere.paths import bin_dir

    return str(bin_dir() / "talk-anywhere")


def _engine_env(*, offline: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else src + ":" + env["PYTHONPATH"]
    env["TALK_ANYWHERE_ROOT"] = str(ROOT)
    env["TALK_ANYWHERE_MODEL"] = str(directory(get_model("pro")))
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TRANSFORMERS_VERBOSITY"] = "error"
    env["TOKENIZERS_PARALLELISM"] = "false"
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
    else:
        env.pop("HF_HUB_OFFLINE", None)
    return env


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _setup_logging() -> None:
    path = log_path()
    if path.exists() and path.stat().st_size > 2_000_000:
        backup = path.with_name("app.log.1")
        backup.unlink(missing_ok=True)
        path.rename(backup)
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def run() -> None:
    _setup_logging()
    if not os.environ.get("DISPLAY"):
        raise SystemExit("No DISPLAY. Talk Anywhere needs the GNOME X11 session.")
    session = os.environ.get("XDG_SESSION_TYPE", "x11")
    if session not in ("x11", "tty"):
        raise SystemExit(f"This session is {session}. Talk Anywhere types with xdotool, which needs X11.")
    GLib.set_prgname("talk-anywhere")
    Gtk.init([])
    app = TalkApp()
    try:
        app.server = CommandServer(app.on_command)
    except RuntimeError:
        log.info("another instance is already running")
        return
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, app.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, app.quit)
    Gtk.main()
