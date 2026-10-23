"""contract.py, the MCP tools and the CLI on an index of the made-up storefront
(tests/fixtures/storefront): a consumer that serves a checkout API, calls an order service with
Feign and a stock service with WebClient, and has the order service's spec in contracts/."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_context_graph import server
from test_context_graph.cli import main
from test_context_graph.context import ContextEngine, ContextError
from test_context_graph.contract import ContractContext, render_call_markdown, render_calls_markdown
from test_context_graph.embeddings import HashEmbedder
from test_context_graph.indexer import index_repo

STOREFRONT = Path(__file__).parent / "fixtures" / "storefront"
P = "com.example.storefront"
PLACE_ORDER = f"http_call:{P}.client.OrderClient#placeOrder(String,NewOrder)@8"
RESERVE = f"http_call:{P}.client.StockClient#reserve(String,int)@12"


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("contract")
    repo = Path(shutil.copytree(STOREFRONT, root / "storefront"))
    index_repo(repo, root / "index", HashEmbedder())
    return root / "index"


@pytest.fixture(scope="module")
def contract(index_dir: Path) -> ContractContext:
    return ContractContext(ContextEngine(index_dir))


@pytest.fixture(scope="module")
def place_order(contract: ContractContext) -> dict:
    return contract.call_context("POST /orders")


# ---- list ---------------------------------------------------------------------------------


def test_outbound_calls_are_grouped_by_base_url_property(contract):
    result = contract.outbound_calls()
    assert result["count"] == 3
    by_provider = {p["provider"]: p for p in result["providers"]}
    assert set(by_provider) == {"orders.api.url", "stock.api.url"}
    orders = by_provider["orders.api.url"]
    assert [c["call"] for c in orders["calls"]] == ["POST /orders", "GET /orders/{orderId}"]
    assert orders["calls"][0]["provider_operation"] == [
        "POST /orders (contracts/orders-api.yml, placeOrder)"
    ]
    assert {(c["value"], c["profile"]) for c in orders["config"]} == {
        ("${ORDERS_API_URL:http://localhost:8091}", None),
        ("http://orders:8080", "docker"),
    }


def test_outbound_calls_of_a_symbol_follow_calls_through_interfaces(contract):
    assert {c["id"] for p in contract.outbound_calls("CheckoutController#checkout")["providers"]
            for c in p["calls"]} == {PLACE_ORDER, RESERVE}  # fmt: skip
    listener = contract.outbound_calls("RestockListener")
    assert [c["id"] for p in listener["providers"] for c in p["calls"]] == [RESERVE]


def test_secret_config_values_are_masked(index_dir):
    engine = ContextEngine(index_dir)
    values = {n.attrs["key"]: n.attrs["value"] for n in engine.nodes.values() if n.kind == "config"}
    assert values["stock.api.password"] == "***"


# ---- one call -----------------------------------------------------------------------------


def test_call_context_request_and_types(place_order):
    assert place_order["id"] == PLACE_ORDER
    request = place_order["request"]
    assert (request["method"], request["path"], request["headers"]) == (
        "POST", "/orders", ["Idempotency-Key"]
    )
    (new_order,) = request["body"]["types"]
    assert new_order["fields"] == [
        {"name": "sku", "type": "String"},
        {"name": "quantity", "type": "int"},
        {"name": "shipTo", "type": "Address"},
    ]
    assert place_order["response"]["type"] == "OrderReceipt"


def test_call_context_provider_operation_and_overrides(place_order):
    (operation,) = place_order["provider_operations"]
    assert operation["spec"] == "contracts/orders-api.yml"
    assert operation["operation_id"] == "placeOrder"
    assert operation["spec_role"] == "provider"
    assert "201" in operation["responses"]
    assert place_order["specmatic"]["mock_base_url_overrides"] == [
        "orders.api.url=http://localhost:9000"
    ]


def test_entry_points_walk_up_to_the_endpoint_the_system_serves(place_order):
    (entry,) = place_order["entry_points"]
    assert entry["kind"] == "endpoint"
    assert entry["endpoint"] == "POST /api/checkout"
    assert entry["chain"] == [
        "CheckoutController#checkout(String,int)",
        "CheckoutServiceImpl#checkout(String,int)",
        "OrderClient#placeOrder(String,NewOrder)",
    ]


def test_entry_points_include_listeners(contract):
    kinds = {e["kind"] for e in contract.call_context(RESERVE)["entry_points"]}
    assert kinds == {"endpoint", "KafkaListener"}


def test_draft_example_from_the_code_and_the_spec(place_order):
    draft = place_order["specmatic"]["draft_example"]
    assert draft == {
        "http-request": {
            "method": "POST",
            "path": "/orders",
            "headers": {"Idempotency-Key": "(string)"},
            "body": {"sku": "(string)", "quantity": "(number)", "shipTo": {"city": "(string)"}},
        },
        "http-response": {"status": 201, "body": {"id": "(number)", "status": "(string)"}},
    }


def test_a_call_without_a_provider_spec_says_where_to_find_one(contract):
    ctx = contract.call_context(RESERVE)
    assert ctx["provider_operations"] == []
    assert any("find_openapi_spec" in note for note in ctx["notes"])


def test_pick_call_by_id_name_or_method(contract):
    assert contract.pick_call(PLACE_ORDER.removeprefix("http_call:")).id == PLACE_ORDER
    assert contract.pick_call("StockClient#reserve").id == RESERVE
    with pytest.raises(ContextError, match="matches 2 calls"):
        contract.pick_call("OrderClient")
    with pytest.raises(ContextError, match="no outbound HTTP call"):
        contract.pick_call("DELETE /nothing")


def test_markdown_renders(contract, place_order):
    calls = render_calls_markdown(contract.outbound_calls())
    assert "## orders.api.url" in calls and "POST /orders" in calls
    text = render_call_markdown(place_order)
    assert "orders.api.url=http://localhost:9000" in text
    assert "POST /api/checkout" in text and '"Idempotency-Key": "(string)"' in text


# ---- MCP and CLI --------------------------------------------------------------------------


@pytest.fixture
def use_index(index_dir, monkeypatch):
    monkeypatch.setenv("TCG_INDEX", str(index_dir))
    server.reset_engine()
    yield
    server.reset_engine()


def test_mcp_tools(use_index):
    assert "POST /orders" in server.list_outbound_calls()
    assert server.list_outbound_calls(format="json")["count"] == 3
    assert server.get_contract_context("POST /orders", format="json")["id"] == PLACE_ORDER


def test_cli_calls_and_contract(index_dir, capsys):
    assert main(["calls", "--index", str(index_dir)]) == 0
    assert "stock.api.url" in capsys.readouterr().out
    assert main(["contract", "POST /orders", "--index", str(index_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == PLACE_ORDER


def test_a_discovered_feign_client_is_pointed_at_the_mock_by_its_client_url(tmp_path):
    repo = tmp_path / "accounts"
    source = repo / "src/main/java/org/acc/StatsClient.java"
    source.parent.mkdir(parents=True)
    source.write_text('''package org.acc;
@FeignClient(name = "statistics-service")
public interface StatsClient {
    @PutMapping("/statistics/{name}") void update(@PathVariable("name") String name);
}
''')
    index_repo(repo, tmp_path / "index", HashEmbedder())
    ctx = ContractContext(ContextEngine(tmp_path / "index")).call_context("PUT /statistics/{name}")
    assert ctx["specmatic"]["mock_base_url_overrides"] == [
        "spring.cloud.openfeign.client.config.statistics-service.url=http://localhost:9000"
    ]


STOCK_SPEC = """openapi: 3.0.1
info: {title: Stock API, version: 1.0.0}
paths:
  /stock/{sku}/reservations:
    post:
      operationId: reserveStock
      responses:
        "201": {description: Reserved}
"""


def test_a_provider_spec_from_elsewhere_links_calls_and_is_kept(tmp_path):
    repo = Path(shutil.copytree(STOREFRONT, tmp_path / "storefront"))
    spec = tmp_path / "downloads" / "stock-api.yaml"
    spec.parent.mkdir()
    spec.write_text(STOCK_SPEC)
    index = tmp_path / "index"
    index_repo(repo, index, HashEmbedder(), provider_specs=[spec])
    ctx = ContractContext(ContextEngine(index)).call_context(RESERVE)
    (operation,) = ctx["provider_operations"]
    assert (operation["spec"], operation["spec_role"], operation["operation_id"]) == (
        "provider-specs/stock-api.yaml", "provider", "reserveStock"
    )
    assert ctx["specmatic"]["draft_example"]["http-response"]["status"] == 201
    spec.unlink()  # a later run without the flag keeps it: it was copied into the index
    index_repo(repo, index, HashEmbedder())
    assert ContractContext(ContextEngine(index)).call_context(RESERVE)["provider_operations"]


def test_a_missing_provider_spec_is_an_error(tmp_path):
    repo = Path(shutil.copytree(STOREFRONT, tmp_path / "storefront"))
    with pytest.raises(FileNotFoundError):
        index_repo(repo, tmp_path / "index", HashEmbedder(), provider_specs=[tmp_path / "nope.yaml"])


def test_draft_types_uuid_and_dates_and_explains_the_mock_root(tmp_path):
    repo = tmp_path / "shop"
    java = repo / "src/main/java/org/shop"
    java.mkdir(parents=True)
    (java / "PayClient.java").write_text('''package org.shop;
import java.util.UUID;
import java.time.OffsetDateTime;
@HttpExchange("/payments")
public interface PayClient {
    @PostExchange Payment create(@RequestHeader("Idempotency-Key") UUID idempotencyKey, @RequestBody Payment p);
}
''')
    (java / "Payment.java").write_text('''package org.shop;
import java.util.UUID;
import java.time.OffsetDateTime;
public record Payment(UUID paymentId, OffsetDateTime createdAt, java.time.LocalDate on) {}
''')
    (java / "Config.java").write_text('''package org.shop;
@Configuration
public class Config {
    @Bean PayClient payClient(RestClient.Builder b, @Value("${pay.url}") String url) {
        return HttpServiceProxyFactory.builderFor(RestClientAdapter.create(b.baseUrl(url).build())).build()
                .createClient(PayClient.class);
    }
}
''')
    resources = repo / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "application.properties").write_text("pay.url=http://localhost:8081/payments/v1\n")
    index_repo(repo, tmp_path / "index", HashEmbedder())
    ctx = ContractContext(ContextEngine(tmp_path / "index")).call_context("POST /payments")
    draft = ctx["specmatic"]["draft_example"]
    assert draft["http-request"]["headers"] == {"Idempotency-Key": "(uuid)"}
    assert draft["http-request"]["body"] == {"paymentId": "(uuid)", "createdAt": "(datetime)", "on": "(date)"}
    assert ctx["specmatic"]["mock_base_url_overrides"] == ["pay.url=http://localhost:9000"]
    assert any("serves the spec's paths at its root" in n for n in ctx["notes"])
