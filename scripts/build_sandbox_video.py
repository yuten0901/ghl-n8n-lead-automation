"""実HighLevel Sandbox画像から、字幕付きの公開用短編動画を生成する。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "docs" / "assets"
SUBTITLES = REPO / "docs" / "video" / "ghl-sandbox-walkthrough.srt"
OUTPUT = REPO / "docs" / "video" / "ghl-sandbox-walkthrough.mp4"

SLIDES = (
    ("ghl-sandbox-opportunities.png", 8),
    ("ghl-sandbox-contact-detail.png", 20),
    ("ghl-sandbox-opportunities.png", 20),
    ("ghl-sandbox-pipeline.png", 15),
    ("ghl-sandbox-contacts.png", 15),
    ("ghl-sandbox-contact-detail.png", 12),
)


def _ffmpeg_path(path: Path) -> str:
    """concat demuxer向けにWindowsパスをスラッシュ表記へ変換する。"""
    return path.resolve().as_posix().replace("'", "'\\''")


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to build the walkthrough video.")

    missing = [str(ASSETS / name) for name, _ in SLIDES if not (ASSETS / name).is_file()]
    if not SUBTITLES.is_file():
        missing.append(str(SUBTITLES))
    if missing:
        raise SystemExit("Missing video inputs:\n" + "\n".join(missing))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="leadops-video-") as temp_dir:
        concat_file = Path(temp_dir) / "slides.txt"
        lines: list[str] = []
        for filename, seconds in SLIDES:
            lines.extend((f"file '{_ffmpeg_path(ASSETS / filename)}'", f"duration {seconds}"))
        # concat demuxerは最後のdurationを反映するため、最終画像をもう一度要求する。
        lines.append(f"file '{_ffmpeg_path(ASSETS / SLIDES[-1][0])}'")
        concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        subtitle_filter = (
            "subtitles=docs/video/ghl-sandbox-walkthrough.srt:"
            "force_style='FontName=Arial,FontSize=14,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,"
            "MarginL=90,MarginR=90,MarginV=30,Alignment=2'"
        )
        command = [
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-t",
            "90",
            "-vf",
            subtitle_filter,
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(OUTPUT),
        ]
        subprocess.run(command, cwd=REPO, check=True)

    print(f"Built {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
