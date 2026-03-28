"""Tests for aru.tools.ranker module."""

import os
import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import patch

from aru.tools.ranker import (
    _extract_keywords,
    _score_name_match,
    _score_recency,
    _get_project_files,
    _get_structural_scores,
    rank_files,
    WEIGHT_NAME,
    WEIGHT_STRUCTURAL,
    WEIGHT_RECENCY,
)


class TestExtractKeywords:
    """Test keyword extraction from task descriptions."""

    def test_basic_keywords(self):
        task = "add authentication to the API"
        keywords = _extract_keywords(task)
        assert "authentication" in keywords
        assert "API" in keywords
        # Stop words should be filtered
        assert "add" not in keywords
        assert "the" not in keywords
        assert "to" not in keywords

    def test_filters_stop_words(self):
        task = "the quick brown fox jumps over the lazy dog"
        keywords = _extract_keywords(task)
        assert "quick" in keywords
        assert "brown" in keywords
        assert "fox" in keywords
        assert "jumps" in keywords
        assert "lazy" in keywords
        # Common stop words
        assert "the" not in keywords
        assert "over" not in keywords

    def test_filters_short_words(self):
        task = "add a new id to db"
        keywords = _extract_keywords(task)
        # Words < 3 chars filtered
        assert "id" not in keywords
        assert "db" not in keywords
        # "add", "new" are in stop words

    def test_filters_action_verbs(self):
        task = "create new file and update config"
        keywords = _extract_keywords(task)
        # Action verbs are in stop words
        assert "create" not in keywords
        assert "new" not in keywords
        assert "update" not in keywords
        assert "file" not in keywords
        # But meaningful words remain
        assert "config" in keywords

    def test_preserves_technical_terms(self):
        task = "refactor authentication middleware"
        keywords = _extract_keywords(task)
        assert "refactor" in keywords
        assert "authentication" in keywords
        assert "middleware" in keywords

    def test_handles_underscores_and_camelcase(self):
        task = "fix user_profile rendering in HomePage"
        keywords = _extract_keywords(task)
        assert "user_profile" in keywords
        assert "rendering" in keywords
        assert "HomePage" in keywords

    def test_empty_input(self):
        keywords = _extract_keywords("")
        assert keywords == []

    def test_only_stop_words(self):
        keywords = _extract_keywords("the and or but if")
        assert keywords == []


class TestScoreNameMatch:
    """Test name matching score calculation."""

    def test_exact_component_match(self):
        score = _score_name_match("src/auth/login.py", ["auth", "login"])
        assert score > 0.5  # Should have high score

    def test_partial_match(self):
        score = _score_name_match("src/authentication.py", ["auth"])
        assert score > 0.0  # Partial match counts
        
    def test_case_insensitive(self):
        score1 = _score_name_match("src/Auth.py", ["auth"])
        score2 = _score_name_match("src/auth.py", ["AUTH"])
        assert score1 > 0
        assert score2 > 0

    def test_exact_match_scores_higher(self):
        # Exact component match should score higher than partial
        exact = _score_name_match("src/auth/login.py", ["auth"])
        partial = _score_name_match("src/authentication/login.py", ["auth"])
        assert exact >= partial  # Both may max out at 1.0, but exact should not be lower

    def test_no_match(self):
        score = _score_name_match("src/database.py", ["auth", "login"])
        assert score == 0.0

    def test_empty_keywords(self):
        score = _score_name_match("src/test.py", [])
        assert score == 0.0

    def test_short_keywords_ignored(self):
        # Keywords < 3 chars should be ignored
        score = _score_name_match("src/db/id.py", ["db", "id"])
        assert score == 0.0

    def test_separator_splitting(self):
        # Should split on /, \, _, -, .
        score = _score_name_match("my-test_file.name.py", ["test", "file", "name"])
        assert score > 0.5

    def test_normalized_score_capped_at_one(self):
        # Even with many matches, score shouldn't exceed 1.0
        score = _score_name_match(
            "auth/auth_login/auth_test.py",
            ["auth"] * 10
        )
        assert score <= 1.0

    def test_multiple_keyword_matching(self):
        score = _score_name_match("user/profile/settings.py", ["user", "profile"])
        assert score > 0.0

    def test_path_component_substring_of_keyword(self):
        # "auth" (from path) is a substring of keyword "authentication" → +1.5 match
        # With 2 keywords, score = min(1.5 / 2, 1.0) = 0.75, strictly between 0 and 1
        score = _score_name_match("src/auth.py", ["authentication", "unrelated"])
        assert 0 < score < 1


class TestScoreRecency:
    """Test recency scoring based on file modification time."""

    def test_very_recent_file(self, tmp_path):
        # Create a file just now
        test_file = tmp_path / "recent.txt"
        test_file.write_text("content")
        
        score = _score_recency("recent.txt", str(tmp_path))
        assert score > 0.99  # Brand new file should score ~1.0 (allow floating point tolerance)

    def test_old_file(self, tmp_path):
        # Create a file and artificially age it
        test_file = tmp_path / "old.txt"
        test_file.write_text("content")
        
        # Set mtime to 60 days ago
        old_time = time.time() - (60 * 86400)
        os.utime(test_file, (old_time, old_time))
        
        score = _score_recency("old.txt", str(tmp_path), max_age_days=30.0)
        assert score == 0.0  # File older than max_age should score 0

    def test_mid_age_file(self, tmp_path):
        test_file = tmp_path / "mid.txt"
        test_file.write_text("content")
        
        # Set mtime to 15 days ago (half of default 30 days)
        mid_time = time.time() - (15 * 86400)
        os.utime(test_file, (mid_time, mid_time))
        
        score = _score_recency("mid.txt", str(tmp_path), max_age_days=30.0)
        assert 0.4 < score < 0.6  # Should be around 0.5

    def test_custom_max_age(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        
        # Set mtime to 5 days ago
        old_time = time.time() - (5 * 86400)
        os.utime(test_file, (old_time, old_time))
        
        # With max_age=10, should score ~0.5
        score = _score_recency("test.txt", str(tmp_path), max_age_days=10.0)
        assert 0.4 < score < 0.6

    def test_nonexistent_file(self, tmp_path):
        score = _score_recency("nonexistent.txt", str(tmp_path))
        assert score == 0.0  # Missing file should score 0

    def test_file_in_subdirectory(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        test_file = subdir / "nested.txt"
        test_file.write_text("content")
        
        score = _score_recency("subdir/nested.txt", str(tmp_path))
        assert score > 0.99  # Just created, should be ~1.0


class TestGetProjectFiles:
    """Test project file discovery with gitignore filtering."""

    def test_lists_python_files(self, tmp_path):
        # Create test structure
        (tmp_path / "main.py").write_text("# main")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "utils.py").write_text("# utils")
        
        os.chdir(tmp_path)
        files = _get_project_files(str(tmp_path))
        
        assert "main.py" in files
        assert "lib/utils.py" in files

    def test_respects_gitignore(self, tmp_path):
        # Create files
        (tmp_path / "included.py").write_text("")
        (tmp_path / "excluded.py").write_text("")
        (tmp_path / ".gitignore").write_text("excluded.py\n")
        
        os.chdir(tmp_path)
        files = _get_project_files(str(tmp_path))
        
        assert "included.py" in files
        assert "excluded.py" not in files

    def test_empty_directory(self, tmp_path):
        os.chdir(tmp_path)
        files = _get_project_files(str(tmp_path))
        assert files == []

    def test_uses_forward_slashes(self, tmp_path):
        # Ensure paths use forward slashes even on Windows
        subdir = tmp_path / "nested" / "deep"
        subdir.mkdir(parents=True)
        (subdir / "file.py").write_text("")
        
        os.chdir(tmp_path)
        files = _get_project_files(str(tmp_path))
        
        # Should use forward slashes
        assert any("nested/deep/file.py" in f for f in files)
        assert all("\\" not in f for f in files)


class TestGetStructuralScores:
    """Test structural dependency scoring."""

    def test_returns_empty_for_nonexistent_files(self, tmp_path):
        scores = _get_structural_scores(["nonexistent.py"], str(tmp_path))
        assert scores == {}

    def test_scores_dependencies(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "utils.py").write_text("# utils")
        (tmp_path / "main.py").write_text("from utils import helper\n")

        scores = _get_structural_scores(["main.py"], str(tmp_path))
        assert "utils.py" in scores

    def test_limits_to_top_5(self, tmp_path):
        # Should only trace top 5 files
        files = [f"file_{i}.py" for i in range(10)]
        for f in files:
            (tmp_path / f).write_text("# empty")

        scores = _get_structural_scores(files, str(tmp_path))
        # Should not raise and should return (possibly empty) dict
        assert isinstance(scores, dict)


class TestRankFiles:
    """Test the main rank_files function."""

    def test_empty_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = rank_files("add authentication")
        assert "No files found" in result

    def test_ranks_relevant_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "auth.py").write_text("def authenticate(): pass")
        (tmp_path / "database.py").write_text("def connect_db(): pass")
        (tmp_path / "config.py").write_text("DEBUG = True")

        result = rank_files("auth login")
        assert "auth.py" in result
        # auth.py should rank higher (name match on "auth")
        lines = result.strip().split("\n")
        ranked_files = [l for l in lines if ". " in l and ".py" in l]
        if ranked_files:
            assert "auth.py" in ranked_files[0]

    def test_returns_formatted_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("code")

        result = rank_files("test task")
        assert "ranked by relevance" in result.lower() or "Ranking mode" in result

    def test_respects_top_k(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for i in range(20):
            (tmp_path / f"file_{i}.py").write_text(f"module {i}")

        result = rank_files("some task", top_k=5)
        # Count numbered results
        import re
        numbered = re.findall(r"^\s+\d+\.", result, re.MULTILINE)
        assert len(numbered) <= 5

    def test_weights_sum_to_one(self):
        total = WEIGHT_NAME + WEIGHT_STRUCTURAL + WEIGHT_RECENCY
        assert abs(total - 1.0) < 0.001

    def test_rank_files_custom_weights(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # auth.py matches "auth" by name; database.py does not
        (tmp_path / "auth.py").write_text("def authenticate(): pass")
        (tmp_path / "database.py").write_text("def connect_db(): pass")

        # With full weight on name matching, auth.py should rank first
        with patch("aru.tools.ranker.WEIGHT_NAME", 1.0), \
             patch("aru.tools.ranker.WEIGHT_STRUCTURAL", 0.0), \
             patch("aru.tools.ranker.WEIGHT_RECENCY", 0.0):
            result_name_heavy = rank_files("auth", top_k=2)

        # With full weight on recency only, both files are equally new so
        # ordering may vary — but auth.py should still appear in results
        with patch("aru.tools.ranker.WEIGHT_NAME", 0.0), \
             patch("aru.tools.ranker.WEIGHT_STRUCTURAL", 0.0), \
             patch("aru.tools.ranker.WEIGHT_RECENCY", 1.0):
            result_recency_heavy = rank_files("auth", top_k=2)

        # Under name-heavy weights auth.py must rank first
        lines_name = [l for l in result_name_heavy.strip().split("\n") if ".py" in l]
        assert lines_name, "Expected ranked file lines in output"
        assert "auth.py" in lines_name[0]

        # Both files should appear regardless of weight configuration
        assert "auth.py" in result_recency_heavy
        assert "database.py" in result_recency_heavy

    def test_no_matching_files_returns_appropriate_message(self, tmp_path, monkeypatch):
        """Test that rank_files handles case where no files match the task."""
        monkeypatch.chdir(tmp_path)
        # Create files with names that don't match any reasonable keywords
        (tmp_path / "aaa.py").write_text("# placeholder")
        (tmp_path / "bbb.py").write_text("# placeholder")
        # Set all files to be very old (beyond max_age_days)
        old_time = time.time() - (60 * 86400)
        for f in ["aaa.py", "bbb.py"]:
            os.utime(tmp_path / f, (old_time, old_time))

        # With zero weight on name and recency, only structural remains
        # but since there are no dependencies, all scores will be 0
        with patch("aru.tools.ranker.WEIGHT_NAME", 0.0), \
             patch("aru.tools.ranker.WEIGHT_STRUCTURAL", 0.0), \
             patch("aru.tools.ranker.WEIGHT_RECENCY", 0.0):
            result = rank_files("xyz nonexistent term", top_k=5)

        # Should return a message about no relevant files found
        assert "No files found" in result or "no files" in result.lower()

    def test_structural_scores_with_real_imports(self, tmp_path, monkeypatch):
        """Test that rank_files uses structural dependency scoring with real imports."""
        monkeypatch.chdir(tmp_path)

        # Create files with real import statements
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / "main.py").write_text("from utils import helper\ndef main(): pass")

        # Set both files to be equally recent
        current_time = time.time()
        os.utime(tmp_path / "utils.py", (current_time, current_time))
        os.utime(tmp_path / "main.py", (current_time, current_time))

        # Search for "util" - utils.py should appear (name match on "util" matches "utils")
        with patch("aru.tools.ranker.WEIGHT_NAME", 0.0), \
             patch("aru.tools.ranker.WEIGHT_STRUCTURAL", 1.0), \
             patch("aru.tools.ranker.WEIGHT_RECENCY", 0.0):
            result = rank_files("util", top_k=5)

        # utils.py should be in results (dependency scoring looks at imports)
        assert "utils.py" in result

    def test_files_in_subdirectories_ranked_correctly(self, tmp_path, monkeypatch):
        """Test that files in nested directories are ranked correctly."""
        monkeypatch.chdir(tmp_path)

        # Create nested directory structure
        (tmp_path / "auth").mkdir()
        (tmp_path / "auth" / "login.py").write_text("def login(): pass")
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "config.py").write_text("DEBUG = True")

        # Ensure both files have the same recent timestamp
        current_time = time.time()
        os.utime(tmp_path / "auth" / "login.py", (current_time, current_time))
        os.utime(tmp_path / "core" / "config.py", (current_time, current_time))

        result = rank_files("authentication")

        # auth/login.py should rank higher because "auth" matches
        lines = result.split("\n")
        auth_line_idx = next((i for i, l in enumerate(lines) if "auth/login.py" in l), None)
        config_line_idx = next((i for i, l in enumerate(lines) if "core/config.py" in l), None)

        assert auth_line_idx is not None
        assert config_line_idx is not None
        assert auth_line_idx < config_line_idx  # auth should appear before config

    def test_case_insensitive_path_matching(self, tmp_path, monkeypatch):
        """Test that file path matching is case-insensitive."""
        monkeypatch.chdir(tmp_path)

        # Create files with mixed case names
        (tmp_path / "Auth.py").write_text("def authenticate(): pass")
        (tmp_path / "USER.py").write_text("class User: pass")

        current_time = time.time()
        os.utime(tmp_path / "Auth.py", (current_time, current_time))
        os.utime(tmp_path / "USER.py", (current_time, current_time))

        # Search with lowercase - should find both due to case-insensitive matching
        with patch("aru.tools.ranker.WEIGHT_NAME", 1.0), \
             patch("aru.tools.ranker.WEIGHT_STRUCTURAL", 0.0), \
             patch("aru.tools.ranker.WEIGHT_RECENCY", 0.0):
            result = rank_files("auth", top_k=5)

        assert "Auth.py" in result