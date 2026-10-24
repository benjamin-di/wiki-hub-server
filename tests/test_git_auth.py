import pytest

from wiki_hub.git_auth import reply


@pytest.mark.parametrize("url", [
    "https://untrusted.example.test/repo", "http://git.example.test/repo",
    "https://git.example.test:444/repo", "https://git.example.test.untrusted.example/repo",
    "https://git.example.test@untrusted.example/repo", "file:///local/path",
])
def test_repository_configuration_cannot_obtain_credentials_for_another_origin(url):
    assert reply(f"Password for '{url}': ", "user", "secret", ["https://git.example.test"]) is None


def test_normal_git_and_lfs_prompts_match_only_the_trusted_origin():
    assert reply("Username for 'https://git.example.test/a/repo.git': ", "user", "secret",
                 ["https://git.example.test"]) == "user"
    assert reply("Password for 'https://user@git.example.test:443/a/repo.git': ", "user", "secret",
                 ["https://git.example.test"]) == "secret"
    assert reply("Enter your password", "user", "secret", ["https://git.example.test"]) is None
    assert reply("Password for 'https://git.example.test': ", "user", "secret", []) is None
