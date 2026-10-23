import pytest

from test_context_graph.config import MASK, file_profile, is_config_file, parse_config


@pytest.mark.parametrize(
    "path, expected",
    [
        ("src/main/resources/application.yml", True),
        ("src/main/resources/application-dev.properties", True),
        ("src/test/resources/bootstrap.yaml", True),
        ("src/main/resources/logback.properties", False),
        ("src/main/resources/openapi.yml", False),
    ],
)
def test_is_config_file(path, expected):
    assert is_config_file(path) is expected


def test_file_profile():
    assert file_profile("x/application-dev.yml") == "dev"
    assert file_profile("x/application.yml") is None


def test_properties_keys_values_and_lines():
    text = (
        "# comment\n"
        "order.api.url=${ORDER_API_URL:http://localhost:8090}\n"
        "! also a comment\n"
        "server.port: 8080\n"
        "long.value = a, \\\n"
        "    b\n"
        "empty.key=\n"
    )
    props = parse_config(text, "src/main/resources/application-local.properties")
    assert [(p.key, p.value, p.line) for p in props] == [
        ("order.api.url", "${ORDER_API_URL:http://localhost:8090}", 2),
        ("server.port", "8080", 4),
        ("long.value", "a, b", 5),
        ("empty.key", "", 7),
    ]
    assert {p.profile for p in props} == {"local"}


def test_yaml_is_flattened_with_lines_and_document_profiles():
    text = (
        "order:\n"
        "  api:\n"
        "    url: http://localhost:8090\n"
        "services:\n"
        "  - name: a\n"
        "---\n"
        "spring:\n"
        "  config:\n"
        "    activate:\n"
        "      on-profile: docker\n"
        "order:\n"
        "  api:\n"
        "    url: http://orders:8080\n"
    )
    props = parse_config(text, "src/main/resources/application.yml")
    found = [(p.key, p.value, p.line, p.profile) for p in props]
    assert ("order.api.url", "http://localhost:8090", 3, None) in found
    assert ("services[0].name", "a", 5, None) in found
    assert ("order.api.url", "http://orders:8080", 13, "docker") in found


def test_secret_values_are_masked_but_bare_placeholders_kept():
    text = "db.password=hunter2\napi.token=${API_TOKEN}\nclient.api-key=${KEY:abc}\nurl=x\n"
    values = {p.key: p.value for p in parse_config(text, "application.properties")}
    assert values == {
        "db.password": MASK,
        "api.token": "${API_TOKEN}",
        "client.api-key": MASK,
        "url": "x",
    }


def test_broken_yaml_is_a_value_error():
    with pytest.raises(ValueError):
        parse_config("a: [\n", "application.yml")
