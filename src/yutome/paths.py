from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yutome.config import AppConfig


def resolve_under(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


@dataclass(frozen=True)
class TranscriptArtifactPaths:
    root: Path
    raw_json: Path
    normalized_jsonl: Path
    transcript_txt: Path
    transcript_md: Path
    transcript_vtt: Path
    transcript_srt: Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    data_dir: Path
    artifacts_dir: Path
    portable_export_dir: Path
    obsidian_export_dir: Path
    logs_dir: Path

    @classmethod
    def from_config(cls, config: AppConfig, *, project_root: Path) -> "ProjectPaths":
        root = project_root.resolve()
        data_dir = resolve_under(root, config.storage.data_dir)
        artifacts_dir = resolve_under(data_dir, config.storage.artifact_root)
        return cls(
            root=root,
            data_dir=data_dir,
            artifacts_dir=artifacts_dir,
            portable_export_dir=data_dir / "exports" / "portable-md",
            obsidian_export_dir=data_dir / "exports" / "obsidian",
            logs_dir=data_dir / "logs",
        )

    def ensure_base_dirs(self) -> None:
        for directory in (
            self.artifacts_dir / "channels",
            self.artifacts_dir / "videos",
            self.portable_export_dir,
            self.obsidian_export_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def channel_artifact(self, channel_id: str) -> Path:
        return self.artifacts_dir / "channels" / channel_id / "channel.json"

    def video_metadata_dir(self, video_id: str) -> Path:
        return self.artifacts_dir / "videos" / video_id / "metadata"

    def transcript_dir(self, video_id: str, transcript_version_id: str) -> Path:
        return (
            self.artifacts_dir
            / "videos"
            / video_id
            / "transcripts"
            / transcript_version_id
        )

    def transcript_artifacts(
        self, video_id: str, transcript_version_id: str
    ) -> TranscriptArtifactPaths:
        root = self.transcript_dir(video_id, transcript_version_id)
        return TranscriptArtifactPaths(
            root=root,
            raw_json=root / "raw.json",
            normalized_jsonl=root / "normalized.jsonl",
            transcript_txt=root / "transcript.txt",
            transcript_md=root / "transcript.md",
            transcript_vtt=root / "transcript.vtt",
            transcript_srt=root / "transcript.srt",
        )

    def chunks_path(self, video_id: str, chunker_version: str) -> Path:
        return self.artifacts_dir / "videos" / video_id / "chunks" / f"{chunker_version}.jsonl"

    def summary_path(self, video_id: str, summary_version: str) -> Path:
        return self.artifacts_dir / "videos" / video_id / "summaries" / f"{summary_version}.json"
