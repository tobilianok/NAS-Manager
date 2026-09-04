import os

import pytest

from app import gitauth


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(gitauth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(gitauth, "TOKEN_FILE", tmp_path / "github_token")


VALID = "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789"


def test_save_and_read_a_token():
    gitauth.save_token(VALID)
    assert gitauth.get_token() == VALID
    assert gitauth.has_token()


def test_token_file_is_readable_by_root_only():
    """Le jeton donne acces au depot : il ne doit jamais etre lisible par
    un autre compte du systeme."""
    gitauth.save_token(VALID)
    mode = os.stat(gitauth.TOKEN_FILE).st_mode & 0o777
    assert mode == 0o600


def test_surrounding_whitespace_is_stripped():
    """Un copier-coller depuis GitHub embarque souvent un retour a la ligne
    ou une espace : ils casseraient l'authentification silencieusement."""
    gitauth.save_token(f"  {VALID}\n")
    assert gitauth.get_token() == VALID


@pytest.mark.parametrize("bad", ["", "   ", "trop-court", "avec espace dedans" * 2,
                                 "jeton;rm -rf /"])
def test_obviously_wrong_tokens_are_refused(bad):
    with pytest.raises(gitauth.GitAuthError):
        gitauth.save_token(bad)
    assert not gitauth.has_token()


def test_no_token_reads_as_absent():
    assert gitauth.get_token() is None
    assert not gitauth.has_token()
    assert gitauth.masked_token() == ""


def test_masked_token_never_shows_the_secret():
    gitauth.save_token(VALID)
    masked = gitauth.masked_token()
    assert VALID not in masked
    assert masked.startswith(VALID[:7])
    assert len(masked) < 20


def test_clear_token_is_idempotent():
    gitauth.clear_token()          # aucun fichier : ne doit pas lever
    gitauth.save_token(VALID)
    gitauth.clear_token()
    assert not gitauth.has_token()


# ---------------------------------------------------------------------------
# Passage a git
# ---------------------------------------------------------------------------

def test_prompting_is_always_disabled():
    """Sans ca, git tente d'ouvrir un terminal inexistant et produit
    'could not read Username ... No such device or address'."""
    assert gitauth.git_env()["GIT_TERMINAL_PROMPT"] == "0"
    gitauth.save_token(VALID)
    assert gitauth.git_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_token_travels_by_environment_not_command_line():
    """Un jeton passe en argument serait visible dans `ps` par n'importe
    quel utilisateur de la machine."""
    gitauth.save_token(VALID)
    assert gitauth.git_env()[gitauth.TOKEN_ENV] == VALID
    args = gitauth.git_config_args()
    assert all(VALID not in arg for arg in args)
    assert any(gitauth.TOKEN_ENV in arg for arg in args)


def test_inherited_credential_helpers_are_cleared_first():
    """Un assistant d'identifiants configure ailleurs (interactif, ou
    casse) reprendrait la main s'il n'etait pas neutralise."""
    gitauth.save_token(VALID)
    args = gitauth.git_config_args()
    assert args[0] == "-c" and args[1] == "credential.helper="


def test_no_credential_helper_without_a_token():
    assert gitauth.git_config_args() == []
    assert gitauth.TOKEN_ENV not in gitauth.git_env()


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

REAL_ERROR = ("fatal: could not read Username for 'https://github.com': "
              "No such device or address")


def test_the_real_error_is_recognised_as_an_authentication_problem():
    assert gitauth.looks_like_auth_failure(REAL_ERROR)


@pytest.mark.parametrize("message", [
    "fatal: unable to access 'https://github.com/': Could not resolve host",
    "error: RPC failed; curl 56 Recv failure",
    "",
])
def test_network_failures_are_not_mistaken_for_authentication(message):
    assert not gitauth.looks_like_auth_failure(message)


def test_explanation_without_a_token_tells_the_user_what_to_do():
    explanation = gitauth.explain_failure(REAL_ERROR, "https://github.com/x/y.git")
    assert "prive" in explanation
    assert "jeton" in explanation
    # Le charabia d'origine ne doit pas etre reservi tel quel.
    assert "No such device" not in explanation


def test_explanation_with_a_token_points_at_the_token_itself():
    gitauth.save_token(VALID)
    explanation = gitauth.explain_failure("remote: Invalid username or token",
                                          "https://github.com/x/y.git")
    assert "expire" in explanation or "LECTURE" in explanation


def test_explanation_for_an_ssh_remote_does_not_suggest_a_token_first():
    """Sur un depot en SSH, un jeton HTTPS ne sert a rien : c'est la cle de
    root qui est utilisee."""
    explanation = gitauth.explain_failure("Permission denied (publickey).",
                                          "git@github.com:x/y.git")
    assert "SSH" in explanation
    assert "root" in explanation


def test_a_non_authentication_message_is_returned_untouched():
    message = "fatal: the remote end hung up unexpectedly"
    assert gitauth.explain_failure(message, "https://github.com/x/y.git") == message
