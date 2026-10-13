from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "YouTube 音视频合并工具"
VIDEO_TYPES = [("视频文件", "*.mp4 *.mkv *.webm *.mov *.avi"), ("所有文件", "*.*")]
AUDIO_TYPES = [("音频文件", "*.m4a *.aac *.mp3 *.webm *.opus *.wav *.ogg"), ("所有文件", "*.*")]
SUBTITLE_TYPES = [("SRT 字幕", "*.srt"), ("所有文件", "*.*")]
MP4_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3"}
MP4_VIDEO_CODECS = {"h264", "hevc", "av1", "mpeg4", "mjpeg", "vp9"}


def hidden_process_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def find_binary(name: str) -> str | None:
    local = Path(__file__).resolve().parent / "tools" / f"{name}.exe"
    if local.exists():
        return str(local)
    return shutil.which(name)


def probe_media(ffmpeg: str, ffprobe: str | None, path: str) -> dict:
    if not ffprobe:
        return probe_media_with_ffmpeg(ffmpeg, path)
    command = [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,duration", "-of", "json", path]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_process_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"无法读取文件：{path}")
    return json.loads(result.stdout)


def probe_media_with_ffmpeg(ffmpeg: str, path: str) -> dict:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_process_kwargs(),
    )
    details = result.stderr
    if "No such file or directory" in details or "Invalid data found" in details:
        raise RuntimeError(f"无法读取媒体文件：{path}")

    duration = 0.0
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", details)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    streams = []
    for stream_type, codec in re.findall(r"Stream #\S+.*?: (Video|Audio):\s*([^,\s]+)", details):
        streams.append({"codec_type": stream_type.lower(), "codec_name": codec.lower()})
    if not streams:
        raise RuntimeError(f"没有在文件中识别到音视频流：{path}")
    return {"format": {"duration": str(duration)}, "streams": streams}


def first_stream(info: dict, stream_type: str) -> dict | None:
    return next((s for s in info.get("streams", []) if s.get("codec_type") == stream_type), None)


def media_duration(info: dict) -> float:
    values: list[float] = []
    raw_format = info.get("format", {}).get("duration")
    if raw_format not in (None, "N/A"):
        try:
            values.append(float(raw_format))
        except (TypeError, ValueError):
            pass
    for stream in info.get("streams", []):
        raw = stream.get("duration")
        if raw not in (None, "N/A"):
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                pass
    return max(values, default=0.0)


def format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def detect_subtitle_encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    if sample.startswith(b"\xff\xfe"):
        return "UTF-16LE"
    if sample.startswith(b"\xfe\xff"):
        return "UTF-16BE"
    try:
        sample.decode("utf-8-sig")
        return "UTF-8"
    except UnicodeDecodeError:
        return "GB18030"


class MergerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x625")
        self.minsize(680, 580)
        self.configure(bg="#f4f5f7")

        self.ffmpeg = find_binary("ffmpeg")
        self.ffprobe = find_binary("ffprobe")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.is_running = False

        self.video_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.subtitle_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="fast")
        self.shortest_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="请选择视频文件和音频文件")
        self.detail_var = tk.StringVar(value="就绪")
        self.progress_var = tk.DoubleVar(value=0)

        self._configure_style()
        self._build_ui()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.ffmpeg:
            self.after(250, self._show_ffmpeg_help)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f5f7")
        style.configure("Header.TFrame", background="#172033")
        style.configure("Header.TLabel", background="#172033", foreground="#ffffff", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subheader.TLabel", background="#172033", foreground="#b8c2d8", font=("Microsoft YaHei UI", 9))
        style.configure("TLabel", background="#f4f5f7", foreground="#202939", font=("Microsoft YaHei UI", 9))
        style.configure("Field.TLabel", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Status.TLabel", foreground="#475467", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(12, 7))
        style.configure("Primary.TButton", background="#176b5b", foreground="#ffffff", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 9))
        style.map("Primary.TButton", background=[("active", "#12594c"), ("disabled", "#9ca3af")])
        style.configure("Danger.TButton", foreground="#b42318")
        style.configure("TRadiobutton", background="#f4f5f7", font=("Microsoft YaHei UI", 9))
        style.configure("TCheckbutton", background="#f4f5f7", font=("Microsoft YaHei UI", 9))
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#dfe3e8", background="#1d806d", bordercolor="#dfe3e8", lightcolor="#1d806d", darkcolor="#1d806d")

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(28, 22))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Header.TLabel").pack(anchor="w")
        ttk.Label(header, text="合并视频、音频，并可选嵌入 SRT 字幕", style="Subheader.TLabel").pack(anchor="w", pady=(5, 0))

        content = ttk.Frame(self, padding=(28, 22))
        content.pack(fill="both", expand=True)

        self._file_row(content, 0, "视频文件", self.video_var, self._choose_video)
        self._file_row(content, 1, "音频文件", self.audio_var, self._choose_audio)
        self._file_row(content, 2, "SRT 字幕（可选）", self.subtitle_var, self._choose_subtitle)
        self._file_row(content, 3, "输出文件", self.output_var, self._choose_output, save=True)

        ttk.Separator(content).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(20, 18))
        ttk.Label(content, text="处理方式", style="Field.TLabel").grid(row=5, column=0, sticky="nw", pady=3)
        modes = ttk.Frame(content)
        modes.grid(row=5, column=1, columnspan=2, sticky="ew")
        ttk.Radiobutton(modes, text="极速合并（推荐，不重编码视频）", value="fast", variable=self.mode_var).pack(anchor="w")
        ttk.Radiobutton(modes, text="兼容模式（转为 H.264 + AAC，耗时较长）", value="compatible", variable=self.mode_var).pack(anchor="w", pady=(7, 0))
        ttk.Checkbutton(content, text="以较短的流为准，避免结尾黑屏或静音", variable=self.shortest_var).grid(row=6, column=1, columnspan=2, sticky="w", pady=(15, 0))

        progress_area = ttk.Frame(content)
        progress_area.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        ttk.Label(progress_area, textvariable=self.status_var, style="Field.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(progress_area, variable=self.progress_var, maximum=100, style="Green.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(9, 6))
        ttk.Label(progress_area, textvariable=self.detail_var, style="Status.TLabel").pack(anchor="w")

        actions = ttk.Frame(content)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        self.merge_button = ttk.Button(actions, text="开始合并", style="Primary.TButton", command=self._start_merge)
        self.merge_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(10, 0))
        self.folder_button = ttk.Button(actions, text="打开输出文件夹", command=self._open_output_folder)
        self.folder_button.pack(side="right")

        content.columnconfigure(1, weight=1)
        content.rowconfigure(7, weight=1)

    def _file_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command, save: bool = False) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 16), pady=7)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        ttk.Button(parent, text="另存为" if save else "浏览", command=command, width=10).grid(row=row, column=2, padx=(10, 0), pady=7)

    def _choose_video(self) -> None:
        path = filedialog.askopenfilename(title="选择视频文件", filetypes=VIDEO_TYPES)
        if path:
            self.video_var.set(path)
            if not self.output_var.get():
                source = Path(path)
                self.output_var.set(str(source.with_name(f"{source.stem}_已合并.mp4")))

    def _choose_audio(self) -> None:
        path = filedialog.askopenfilename(title="选择音频文件", filetypes=AUDIO_TYPES)
        if path:
            self.audio_var.set(path)

    def _choose_subtitle(self) -> None:
        path = filedialog.askopenfilename(title="选择 SRT 字幕文件", filetypes=SUBTITLE_TYPES)
        if path:
            self.subtitle_var.set(path)

    def _choose_output(self) -> None:
        initial = self.output_var.get()
        path = filedialog.asksaveasfilename(
            title="保存合并后的视频",
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4")],
            initialdir=str(Path(initial).parent) if initial else None,
            initialfile=Path(initial).name if initial else "已合并.mp4",
        )
        if path:
            self.output_var.set(path)

    def _validate_paths(self) -> tuple[Path, Path, Path | None, Path] | None:
        video = Path(self.video_var.get().strip().strip('"'))
        audio = Path(self.audio_var.get().strip().strip('"'))
        subtitle_text = self.subtitle_var.get().strip().strip('"')
        subtitle = Path(subtitle_text) if subtitle_text else None
        output = Path(self.output_var.get().strip().strip('"'))
        if not video.is_file():
            messagebox.showerror(APP_TITLE, "请选择有效的视频文件。")
            return None
        if not audio.is_file():
            messagebox.showerror(APP_TITLE, "请选择有效的音频文件。")
            return None
        if subtitle is not None and (not subtitle.is_file() or subtitle.suffix.lower() != ".srt"):
            messagebox.showerror(APP_TITLE, "请选择有效的 SRT 字幕文件，或者将字幕栏留空。")
            return None
        if not str(output) or output.name in ("", "."):
            messagebox.showerror(APP_TITLE, "请设置输出文件。")
            return None
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
            self.output_var.set(str(output))
        input_paths = [video.resolve(), audio.resolve()]
        if subtitle is not None:
            input_paths.append(subtitle.resolve())
        if output.resolve() in input_paths:
            messagebox.showerror(APP_TITLE, "输出文件不能覆盖输入文件。")
            return None
        if output.exists() and not messagebox.askyesno(APP_TITLE, f"文件已存在，是否覆盖？\n\n{output}"):
            return None
        return video, audio, subtitle, output

    def _start_merge(self) -> None:
        if self.is_running:
            return
        if not self.ffmpeg:
            self._show_ffmpeg_help()
            return
        paths = self._validate_paths()
        if paths is None:
            return
        self.is_running = True
        self.progress_var.set(0)
        self.status_var.set("正在检查媒体信息…")
        self.detail_var.set("准备中")
        self.merge_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        threading.Thread(
            target=self._merge_worker,
            args=(*paths, self.mode_var.get(), self.shortest_var.get()),
            daemon=True,
        ).start()

    def _merge_worker(self, video: Path, audio: Path, subtitle: Path | None, output: Path, mode: str, use_shortest: bool) -> None:
        try:
            video_info = probe_media(self.ffmpeg or "ffmpeg", self.ffprobe, str(video))
            audio_info = probe_media(self.ffmpeg or "ffmpeg", self.ffprobe, str(audio))
            video_stream = first_stream(video_info, "video")
            audio_stream = first_stream(audio_info, "audio")
            if not video_stream:
                raise RuntimeError("所选视频文件中没有找到视频流。")
            if not audio_stream:
                raise RuntimeError("所选音频文件中没有找到音频流。")

            duration_candidates = [d for d in (media_duration(video_info), media_duration(audio_info)) if d > 0]
            duration = min(duration_candidates) if use_shortest and duration_candidates else max(duration_candidates, default=0)
            video_codec = video_stream.get("codec_name", "未知")
            audio_codec = audio_stream.get("codec_name", "未知")
            compatible = mode == "compatible"

            command = [self.ffmpeg or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio)]
            if subtitle is not None:
                command += ["-sub_charenc", detect_subtitle_encoding(subtitle), "-i", str(subtitle)]
            command += ["-map", "0:v:0", "-map", "1:a:0"]
            if subtitle is not None:
                command += ["-map", "2:s:0"]
            if compatible or video_codec not in MP4_VIDEO_CODECS:
                command += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
                video_action = f"{video_codec} → H.264"
            else:
                command += ["-c:v", "copy"]
                video_action = f"{video_codec}（直接复制）"

            if compatible or audio_codec not in MP4_AUDIO_CODECS:
                command += ["-c:a", "aac", "-b:a", "192k"]
                audio_action = f"{audio_codec} → AAC"
            else:
                command += ["-c:a", "copy"]
                audio_action = f"{audio_codec}（直接复制）"

            if subtitle is not None:
                command += [
                    "-c:s", "mov_text",
                    "-metadata:s:s:0", "language=chi",
                    "-metadata:s:s:0", "title=中文字幕",
                    "-disposition:s:0", "default",
                ]

            command += ["-map_metadata", "0", "-movflags", "+faststart"]
            if use_shortest and duration > 0:
                command += ["-t", f"{duration:.6f}"]
            elif use_shortest and subtitle is None:
                command.append("-shortest")
            command += ["-progress", "pipe:1", "-nostats", str(output)]

            output.parent.mkdir(parents=True, exist_ok=True)
            self.events.put(("started", (video_action, audio_action, subtitle is not None, duration)))
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **hidden_process_kwargs(),
            )
            assert self.process.stdout is not None
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        current = int(line.split("=", 1)[1]) / 1_000_000
                        percent = min(99.5, current / duration * 100) if duration > 0 else 0
                        self.events.put(("progress", (percent, current, duration)))
                    except ValueError:
                        pass

            stderr = self.process.stderr.read() if self.process.stderr else ""
            code = self.process.wait()
            if code == 0:
                self.events.put(("done", output))
            elif self.is_running:
                raise RuntimeError(stderr.strip() or f"FFmpeg 处理失败（退出码 {code}）。")
            else:
                output.unlink(missing_ok=True)
                self.events.put(("cancelled", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.process = None

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "started":
                    video_action, audio_action, has_subtitle, _ = payload
                    self.status_var.set("正在合并…")
                    subtitle_action = "  |  字幕：SRT → MP4" if has_subtitle else ""
                    self.detail_var.set(f"视频：{video_action}  |  音频：{audio_action}{subtitle_action}")
                elif event == "progress":
                    percent, current, duration = payload
                    self.progress_var.set(percent)
                    self.status_var.set(f"正在合并… {percent:.1f}%")
                    self.detail_var.set(f"{format_seconds(current)} / {format_seconds(duration)}")
                elif event == "done":
                    self.progress_var.set(100)
                    self.status_var.set("合并完成")
                    self.detail_var.set(str(payload))
                    self._set_idle()
                    if messagebox.askyesno(APP_TITLE, "音视频已成功合并。\n\n是否打开输出文件夹？"):
                        self._open_output_folder(select_file=True)
                elif event == "cancelled":
                    self.status_var.set("已取消")
                    self.detail_var.set("未生成完整输出文件")
                    self._set_idle()
                elif event == "error":
                    self.status_var.set("合并失败")
                    self.detail_var.set("请检查输入文件或切换到兼容模式后重试")
                    self._set_idle()
                    messagebox.showerror(APP_TITLE, str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_idle(self) -> None:
        self.is_running = False
        self.merge_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def _cancel(self) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self.status_var.set("正在取消…")
        process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _open_output_folder(self, select_file: bool = False) -> None:
        output_text = self.output_var.get().strip().strip('"')
        target = Path(output_text) if output_text else Path.cwd()
        folder = target.parent if target.suffix else target
        if not folder.exists():
            messagebox.showinfo(APP_TITLE, "输出文件夹尚不存在。")
            return
        if os.name == "nt":
            if select_file and target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def _show_ffmpeg_help(self) -> None:
        messagebox.showerror(
            APP_TITLE,
            "未找到 FFmpeg。\n\n请安装 FFmpeg 并加入 PATH，或者把 ffmpeg.exe 放到本工具的 tools 文件夹中。",
        )

    def _on_close(self) -> None:
        if self.is_running and not messagebox.askyesno(APP_TITLE, "任务仍在运行，确定要退出吗？"):
            return
        self._cancel()
        self.destroy()


if __name__ == "__main__":
    MergerApp().mainloop()
