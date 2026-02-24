"""Prompt template registry for versioned, hashed prompt management."""

import hashlib
import logging
import re
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from btcedu.models.prompt_version import PromptVersion

logger = logging.getLogger(__name__)

# Default templates directory (relative to project root)
TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates"


class PromptRegistry:
    """Manages prompt template versions with content hashing.

    Loads Markdown templates with YAML frontmatter, computes SHA-256
    content hashes, and tracks versions in the database.
    """

    def __init__(self, session: Session, templates_dir: Path | None = None):
        self._session = session
        self._templates_dir = templates_dir or TEMPLATES_DIR

    def get_default(self, name: str) -> PromptVersion | None:
        """Get the current default PromptVersion for a given prompt name.

        Returns None if no version exists or none is marked as default.
        """
        return (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name, PromptVersion.is_default.is_(True))
            .first()
        )

    def register_version(
        self,
        name: str,
        template_path: str | Path,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        notes: str | None = None,
        set_default: bool = False,
    ) -> PromptVersion:
        """Register a new prompt version from a template file.

        Reads the template, parses YAML frontmatter for metadata,
        computes a SHA-256 content hash, and creates a new PromptVersion
        record. If the content hash already exists for this name, returns
        the existing version instead.

        If set_default=True, promotes this version to default.
        Frontmatter metadata (model, temperature, max_tokens) is used
        as fallback when those parameters are not explicitly provided.
        """
        template_path = Path(template_path)
        metadata, body = self.load_template(template_path)
        content_hash = self.compute_hash(body)

        # Check if this exact content already exists for this name
        existing = (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name, PromptVersion.content_hash == content_hash)
            .first()
        )
        if existing:
            logger.info(
                "Prompt %s already registered with hash %s (version %d)",
                name,
                content_hash[:12],
                existing.version,
            )
            if set_default and not existing.is_default:
                self.promote_to_default(existing.id)
            return existing

        # Resolve metadata with explicit params taking precedence
        resolved_model = model or metadata.get("model")
        resolved_temp = temperature if temperature is not None else metadata.get("temperature")
        resolved_max = max_tokens if max_tokens is not None else metadata.get("max_tokens")

        version_num = self._next_version(name)

        pv = PromptVersion(
            name=name,
            version=version_num,
            content_hash=content_hash,
            template_path=str(template_path),
            model=resolved_model,
            temperature=resolved_temp,
            max_tokens=resolved_max,
            is_default=False,
            notes=notes,
        )
        self._session.add(pv)
        self._session.commit()

        logger.info(
            "Registered prompt %s version %d (hash=%s)", name, version_num, content_hash[:12]
        )

        if set_default:
            self.promote_to_default(pv.id)

        return pv

    def promote_to_default(self, version_id: int) -> None:
        """Promote a specific PromptVersion to be the default for its name.

        Demotes the current default (if any) and sets the given version
        as the new default. Raises ValueError if version_id not found.
        """
        pv = self._session.query(PromptVersion).filter(PromptVersion.id == version_id).first()
        if pv is None:
            raise ValueError(f"PromptVersion with id={version_id} not found")

        # Demote current default for this name
        self._session.query(PromptVersion).filter(
            PromptVersion.name == pv.name, PromptVersion.is_default.is_(True)
        ).update({"is_default": False})

        # Promote the target version
        pv.is_default = True
        self._session.commit()

        logger.info("Promoted prompt %s version %d to default", pv.name, pv.version)

    def get_history(self, name: str) -> list[PromptVersion]:
        """Get all versions of a prompt, ordered by version number descending.

        Returns an empty list if no versions exist for the given name.
        """
        return (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name)
            .order_by(PromptVersion.version.desc())
            .all()
        )

    def compute_hash(self, content: str) -> str:
        """Compute SHA-256 hash of prompt content (after stripping frontmatter).

        Strips YAML frontmatter (delimited by ---) before hashing so that
        metadata changes don't invalidate the content hash.
        """
        body = self._strip_frontmatter(content)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def load_template(self, template_path: str | Path) -> tuple[dict, str]:
        """Load a template file and parse its YAML frontmatter.

        Returns (metadata_dict, body_content). The metadata dict contains
        fields from the YAML frontmatter (name, model, temperature, etc.).
        The body is the template content after the frontmatter.
        """
        template_path = Path(template_path)
        content = template_path.read_text(encoding="utf-8")

        metadata = {}
        body = content

        # Parse YAML frontmatter
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, flags=re.DOTALL)
        if match:
            frontmatter_text = match.group(1)
            metadata = yaml.safe_load(frontmatter_text) or {}
            body = content[match.end() :]

        return metadata, body

    def _next_version(self, name: str) -> int:
        """Get the next version number for a prompt name."""
        latest = (
            self._session.query(PromptVersion)
            .filter(PromptVersion.name == name)
            .order_by(PromptVersion.version.desc())
            .first()
        )
        return (latest.version + 1) if latest else 1

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter delimited by --- from content."""
        pattern = r"^---\s*\n.*?\n---\s*\n"
        return re.sub(pattern, "", content, count=1, flags=re.DOTALL).strip()
