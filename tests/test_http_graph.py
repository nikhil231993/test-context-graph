"""Outbound HTTP calls and config in the graph: a made-up storefront that calls an order service
and serves its own API, with the order service's spec checked into the repo."""

from __future__ import annotations

from test_context_graph.config import parse_config
from test_context_graph.graph import build_graph
from test_context_graph.java_parser import parse_java
from test_context_graph.models import (
    CALLS_HTTP,
    CONFIG,
    CONFIGURED_BY,
    HTTP_CALL,
    TARGETS,
    USES,
    ApiOperation,
    ApiSpec,
    Graph,
)

MAIN = "src/main/java/org/store"
P = "org.store"

JAVA = {
    f"{MAIN}/orders/OrderClient.java": """
package org.store.orders;

import org.store.orders.model.NewOrder;
import org.store.orders.model.OrderReceipt;

@FeignClient(name = "orders", url = "${orders.base-url}")
public interface OrderClient {
    @PostMapping("/orders")
    OrderReceipt place(@RequestHeader("Idempotency-Key") String key, @RequestBody NewOrder order);
}
""",
    f"{MAIN}/orders/model/NewOrder.java": """
package org.store.orders.model;
public record NewOrder(String sku, int count) {}
""",
    f"{MAIN}/orders/model/OrderReceipt.java": """
package org.store.orders.model;
public record OrderReceipt(long id) {}
""",
    f"{MAIN}/checkout/CheckoutService.java": """
package org.store.checkout;

import org.store.orders.OrderClient;
import org.store.orders.model.NewOrder;

@Service
public class CheckoutService {
    private final OrderClient orderClient;
    private final RestTemplate restTemplate;
    @Value("${stock.url}") private String stockUrl;

    public CheckoutService(OrderClient orderClient, RestTemplate restTemplate) {
        this.orderClient = orderClient;
        this.restTemplate = restTemplate;
    }

    public long checkout(String sku) {
        restTemplate.getForObject(stockUrl + "/stock/" + sku, String.class);
        return orderClient.place("key", new NewOrder(sku, 1)).id();
    }
}
""",
    f"{MAIN}/web/CheckoutController.java": """
package org.store.web;

@RestController
public class CheckoutController {
    @PostMapping("/orders")
    public String checkout() { return "ok"; }
}
""",
}

ORDER_SPEC = ApiSpec(
    path="contracts/orders.yml",
    base_path="",
    operations=[
        ApiOperation(method="POST", path="/orders", operation_id="placeOrder"),
        ApiOperation(method="GET", path="/orders/{id}", operation_id="getOrder"),
    ],
)
STORE_SPEC = ApiSpec(  # the storefront's own API: POST /orders too, served by the controller
    path="src/main/resources/store.yml",
    operations=[ApiOperation(method="POST", path="/orders", operation_id="checkout")],
)

APPLICATION_YML = """
orders:
  base-url: http://localhost:9000
stock:
  url: http://stock:8080
---
spring.config.activate.on-profile: docker
orders:
  base-url: http://orders:8080
"""


def build() -> Graph:
    files = [parse_java(text, path) for path, text in JAVA.items()]
    config = parse_config(APPLICATION_YML, "src/main/resources/application.yml")
    return build_graph(files, [ORDER_SPEC, STORE_SPEC], [], config)


def edges(graph: Graph, kind: str) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges if e.kind == kind}


FEIGN_METHOD = f"method:{P}.orders.OrderClient#place(String,NewOrder)"
FEIGN_CALL = f"http_call:{P}.orders.OrderClient#place(String,NewOrder)@9"
STOCK_CALL = f"http_call:{P}.checkout.CheckoutService#checkout(String)@19"


def test_http_call_nodes_and_calls_http_edges():
    graph = build()
    calls = {n.id: n for n in graph.nodes if n.kind == HTTP_CALL}
    assert set(calls) == {FEIGN_CALL, STOCK_CALL}
    assert calls[FEIGN_CALL].name == "POST /orders"
    assert calls[FEIGN_CALL].attrs["headers"] == ["Idempotency-Key"]
    assert calls[STOCK_CALL].name == "GET /stock/{}"
    assert edges(graph, CALLS_HTTP) == {
        (FEIGN_METHOD, FEIGN_CALL),
        (f"method:{P}.checkout.CheckoutService#checkout(String)", STOCK_CALL),
    }


def test_config_nodes_per_profile_and_base_url_edges():
    graph = build()
    config = {n.id: n for n in graph.nodes if n.kind == CONFIG}
    yml = "src/main/resources/application.yml"
    assert config[f"config:{yml}#orders.base-url"].attrs["value"] == "http://localhost:9000"
    assert config[f"config:{yml}#orders.base-url@docker"].attrs["profile"] == "docker"
    assert edges(graph, CONFIGURED_BY) == {
        (FEIGN_CALL, f"config:{yml}#orders.base-url"),
        (FEIGN_CALL, f"config:{yml}#orders.base-url@docker"),
        (STOCK_CALL, f"config:{yml}#stock.url"),
    }


def test_targets_link_to_the_provider_spec_not_to_the_own_api():
    graph = build()
    provider_post = "endpoint:POST /orders@contracts/orders.yml"
    assert edges(graph, TARGETS) == {(FEIGN_CALL, provider_post)}
    target = next(e for e in graph.edges if e.kind == TARGETS)
    assert target.attrs == {"match": "provider_spec"}
    # the storefront's own POST /orders is a separate node, served by its controller
    assert (f"method:{P}.web.CheckoutController#checkout()", "endpoint:POST /orders") in edges(
        graph, "SERVES"
    )
    assert not any(t == provider_post for _, t in edges(graph, "SERVES"))


def test_an_unserved_own_spec_endpoint_is_a_fallback_target():
    files = [parse_java(text, path) for path, text in JAVA.items()]
    elsewhere = ApiSpec(path="api/stock.yml", operations=[ApiOperation(method="GET", path="/stock/{sku}")])
    graph = build_graph(files, [elsewhere], [], [])
    assert edges(graph, TARGETS) == {(STOCK_CALL, "endpoint:GET /stock/{sku}")}


def test_request_and_response_types_resolve_to_project_types():
    graph = build()
    uses = {(s, t) for s, t in edges(graph, USES) if s.startswith("http_call:")}
    assert uses == {
        (FEIGN_CALL, f"type:{P}.orders.model.NewOrder"),
        (FEIGN_CALL, f"type:{P}.orders.model.OrderReceipt"),
    }
    node = next(n for n in graph.nodes if n.id == FEIGN_CALL)
    assert node.attrs["request_type"] == f"type:{P}.orders.model.NewOrder"
    assert node.attrs["response_type"] == f"type:{P}.orders.model.OrderReceipt"


def test_modules_keep_their_own_config_prefixes_beans_and_properties():
    """Two services in one repo, each with an ApplicationProperties of its own prefix and its own
    application.properties: a call links only to its own module's config."""
    def module(name: str, prefix: str) -> list:
        return [
            parse_java(f"""
package org.{name};
@ConfigurationProperties(prefix = "{prefix}")
public record ApplicationProperties(String inventoryUrl) {{}}
""", f"{name}/src/main/java/org/{name}/ApplicationProperties.java"),
            parse_java(f"""
package org.{name};
public class InventoryProxy {{
    private final WebClient webClient;
    public InventoryProxy(WebClient.Builder builder, ApplicationProperties props) {{
        this.webClient = builder.baseUrl(props.inventoryUrl()).build();
    }}
    public String get() {{
        return webClient.get().uri("/inventory").retrieve().bodyToMono(String.class).block();
    }}
}}
""", f"{name}/src/main/java/org/{name}/InventoryProxy.java"),
        ]

    files = module("catalog", "application") + module("webapp", "retailstore")
    config = parse_config("application.inventory-url=http://localhost:1\nretailstore.inventory-url=x\n",
                          "catalog/src/main/resources/application.properties")
    config += parse_config("retailstore.inventory-url=http://localhost:2\n",
                           "webapp/src/main/resources/application.properties")
    graph = build_graph(files, [], [], config)
    calls = {n.path.split("/")[0]: n for n in graph.nodes if n.kind == HTTP_CALL}
    assert calls["catalog"].attrs["base_url"] == "${application.inventory-url}"
    assert calls["webapp"].attrs["base_url"] == "${retailstore.inventory-url}"
    assert edges(graph, CONFIGURED_BY) == {
        (calls["catalog"].id, "config:catalog/src/main/resources/application.properties#application.inventory-url"),
        (calls["webapp"].id, "config:webapp/src/main/resources/application.properties#retailstore.inventory-url"),
    }
