"""Outbound HTTP calls, one client style per test. Made-up code in the style of Spring apps."""

from test_context_graph.http_calls import HttpCallFact, client_env, find_http_calls
from test_context_graph.java_parser import parse_java


def calls(source: str, type_name: str | None = None, extra: str = "") -> list[HttpCallFact]:
    jf = parse_java("package org.shop;\n" + source, "src/main/java/org/shop/X.java")
    env_files = [jf] + ([parse_java("package org.shop;\n" + extra, "src/main/java/org/shop/Y.java")] if extra else [])
    env = client_env(env_files)
    found = []
    for decl in jf.types:
        if type_name is None or decl.name == type_name:
            found += find_http_calls(decl, env)
    return found


def summary(fact: HttpCallFact) -> dict:
    return {k: v for k, v in vars(fact).items() if k != "method" and v not in (None, [], "")}


def test_feign_client_methods_with_params_headers_body_and_base_url():
    found = calls('''
        @FeignClient(name = "orders", url = "${order.api.url}", path = "/v1")
        interface OrderClient {
            @PostMapping("/orders")
            ResponseEntity<OrderReceipt> place(@RequestHeader("Idempotency-Key") String key,
                                               @RequestBody NewOrder order);
            @GetMapping("/orders/{id}")
            OrderReceipt get(@PathVariable("id") long id, @RequestParam("expand") String expand);
            @RequestMapping(method = RequestMethod.DELETE, value = "/orders/{id}")
            void cancel(@PathVariable long id);
        }''')
    assert [summary(f) for f in found] == [
        {"client": "feign", "line": 5, "resolution": "declared", "http_method": "POST",
         "path": "/v1/orders", "headers": ["Idempotency-Key"], "request_body": "NewOrder",
         "response": "OrderReceipt", "base_url": "${order.api.url}",
         "base_url_via": "@FeignClient(url)", "service": "orders"},
        {"client": "feign", "line": 8, "resolution": "declared", "http_method": "GET",
         "path": "/v1/orders/{id}", "query": ["expand"], "response": "OrderReceipt",
         "base_url": "${order.api.url}", "base_url_via": "@FeignClient(url)", "service": "orders"},
        {"client": "feign", "line": 10, "resolution": "declared", "http_method": "DELETE",
         "path": "/v1/orders/{id}", "base_url": "${order.api.url}",
         "base_url_via": "@FeignClient(url)", "service": "orders"},
    ]  # fmt: skip
    assert found[0].property_keys == ["order.api.url"]


def test_feign_without_url_keeps_the_service_name_and_an_unannotated_body():
    (fact,) = calls('''
        @FeignClient("stock")
        interface StockClient {
            @PutMapping("/stock/{sku}") Stock update(@PathVariable String sku, StockChange change);
        }''')
    assert (fact.service, fact.base_url, fact.request_body) == ("stock", None, "StockChange")


def test_an_interface_with_mappings_but_no_client_annotation_is_a_server_api():
    assert calls('''
        interface OrdersApi { @GetMapping("/orders") List<Order> list(); }''') == []


def test_spring_http_interface():
    (fact,) = calls('''
        @HttpExchange(url = "/inventory")
        interface InventoryClient {
            @GetExchange("/items/{sku}") Mono<Item> item(@PathVariable String sku);
        }''')
    assert summary(fact) == {
        "client": "http_interface", "line": 5, "resolution": "declared", "http_method": "GET",
        "path": "/inventory/items/{sku}", "response": "Item",
    }  # fmt: skip


def test_rest_template_concatenated_url_with_value_field_base():
    found = calls('''
        @Service
        class StockService {
            private final RestTemplate restTemplate;
            @Value("${stock.api.url}") private String stockUrl;
            Stock[] find(String type, long id) {
                restTemplate.exchange(stockUrl + "/stock/{id}", HttpMethod.DELETE, null, Void.class, id);
                return restTemplate.getForObject(stockUrl + "/stock?type=" + type, Stock[].class);
            }
            Receipt add(StockChange change) {
                return this.restTemplate.postForObject(stockUrl + "/stock", change, Receipt.class);
            }
        }''')
    assert [(f.http_method, f.path, f.query, f.request_body, f.response) for f in found] == [
        ("DELETE", "/stock/{id}", [], None, None),
        ("GET", "/stock", ["type"], None, "Stock[]"),
        ("POST", "/stock", [], "StockChange", "Receipt"),
    ]
    assert {(f.base_url, f.base_url_via, f.resolution) for f in found} == {
        ("${stock.api.url}", "`stockUrl` in the URL", "literal")
    }


def test_rest_template_with_a_literal_absolute_url_and_a_hidden_url():
    first, second = calls('''
        class Pinger {
            private RestTemplate rest;
            void ping(String url) {
                rest.getForEntity("http://status:8080/health", String.class);
                rest.delete(url);
            }
        }''')
    assert (first.base_url, first.path, first.response) == ("http://status:8080", "/health", "String")
    assert (second.http_method, second.path, second.resolution) == ("DELETE", None, "partial")
    assert second.notes == ["URL from `url`: not visible here"]


def test_web_client_chain_with_uri_lambda_headers_and_bean_base_url():
    (fact,) = calls(
        '''
        @Service
        class ProductService {
            private final WebClient productWebClient;
            List<Product> find(String type) {
                return productWebClient.get()
                    .uri(u -> u.path("/products").queryParam("type", type).build())
                    .header("pageSize", "20")
                    .accept(MediaType.APPLICATION_JSON)
                    .retrieve()
                    .bodyToFlux(Product.class)
                    .collectList().block();
            }
        }''',
        extra='''
        @Configuration
        class Clients {
            @Bean WebClient productWebClient(WebClient.Builder b, @Value("${product.api.url}") String url) {
                return b.baseUrl(url).build();
            }
        }''',
    )
    assert summary(fact) == {
        "client": "web_client", "line": 7, "resolution": "literal", "http_method": "GET",
        "path": "/products", "query": ["type"], "headers": ["pageSize", "Accept"],
        "response": "List<Product>", "base_url": "${product.api.url}",
        "base_url_via": "@Bean Clients#productWebClient: baseUrl(url)", "service": "productWebClient",
    }  # fmt: skip


def test_two_chains_on_one_client_in_one_method_stay_apart():
    first, second = calls('''
        class Sync {
            private WebClient client;
            void run(Order o) {
                client.post().uri("/orders").bodyValue(o).retrieve().toBodilessEntity().block();
                client.get().uri("/orders/{id}", 1).retrieve().bodyToMono(Order.class).block();
            }
        }''')
    assert (first.http_method, first.path, first.request_body, first.response) == ("POST", "/orders", "Order", None)
    assert (second.http_method, second.path, second.request_body, second.response) == ("GET", "/orders/{id}", None, "Order")


def test_rest_client_with_configuration_properties_base_url():
    (fact,) = calls(
        '''
        class PricingClient {
            private final RestClient restClient;
            PricingClient(RestClient.Builder builder, PricingProperties props) {
                this.restClient = builder.baseUrl(props.getBaseUrl()).build();
            }
            Price quote(String sku) {
                return restClient.post().uri("/prices").contentType(MediaType.APPLICATION_JSON)
                    .body(new PriceQuery(sku)).retrieve().body(Price.class);
            }
        }''',
        extra='''
        @ConfigurationProperties(prefix = "pricing.api")
        class PricingProperties { private String baseUrl; }''',
    )
    assert (fact.client, fact.http_method, fact.path, fact.request_body, fact.response) == (
        "rest_client", "POST", "/prices", "PriceQuery", "Price"
    )
    assert fact.headers == ["Content-Type"]
    assert fact.base_url == "${pricing.api.base-url}"
    assert fact.base_url_via == "baseUrl(props.getBaseUrl()) in PricingClient#<init>"


def test_a_builder_base_url_only_applies_to_its_own_client_type():
    found = calls('''
        class Mixed {
            private final RestClient pricing;
            private final RestTemplate legacy;
            Mixed(RestClient.Builder b, @Value("${pricing.url}") String url, RestTemplate legacy) {
                this.pricing = b.baseUrl(url).build();
                this.legacy = legacy;
            }
            void run() {
                pricing.get().uri("/prices").retrieve().body(String.class);
                legacy.getForObject("/old", String.class);
            }
        }''')
    assert [(f.client, f.base_url) for f in found] == [
        ("rest_client", "${pricing.url}"),
        ("rest_template", None),
    ]


def test_bean_methods_and_types_without_clients_give_nothing():
    assert calls('''
        @Configuration
        class Clients {
            @Bean RestTemplate rest(RestTemplateBuilder b) { return b.rootUri("http://x").build(); }
        }
        class Plain { void run() { list.get(0); } }''') == []


def test_http_interface_base_url_from_import_http_services_group():
    (fact,) = calls(
        '''
        interface CatalogProxy {
            @GetExchange("/api/catalog/{code}") Product product(@PathVariable String code);
        }''',
        extra='''
        @Configuration
        @ImportHttpServices(group = "catalog", types = CatalogProxy.class)
        class HttpClientConfig {
            @Bean
            RestClientHttpServiceGroupConfigurer groups(ApplicationProperties props) {
                return groups -> groups.forEachClient((group, builder) -> builder.baseUrl(props.catalogUrl()));
            }
        }
        @ConfigurationProperties("application")
        record ApplicationProperties(String catalogUrl) {}''',
    )
    assert fact.base_url == "${application.catalog-url}"
    assert fact.base_url_via.startswith("@ImportHttpServices HttpClientConfig#groups")


def test_http_interface_base_url_from_a_bean_that_builds_its_proxy():
    (fact,) = calls(
        '''
        interface StockApi { @GetExchange("/stock") List<Stock> all(); }''',
        extra='''
        @Configuration
        class Proxies {
            @Bean StockApi stockApi(@Value("${stock.url}") String url) {
                RestClient client = RestClient.builder().baseUrl(url).build();
                return HttpServiceProxyFactory.builderFor(RestClientAdapter.create(client)).build()
                        .createClient(StockApi.class);
            }
            @Bean OtherApi otherApi() { return null; }
        }''',
    )
    assert (fact.base_url, fact.base_url_via) == ("${stock.url}", "@Bean Proxies#stockApi: baseUrl(url)")


def test_an_unresolved_base_says_where_it_comes_from_and_paths_start_with_a_slash():
    (fact,) = calls('''
        class Loader {
            private final WebClient webClient;
            void load() {
                String host = "http://vets-service/";
                webClient.get().uri(host + "vets").retrieve().bodyToMono(String.class).block();
            }
        }''')
    assert fact.path == "/vets"
    assert fact.base_url is None
    assert fact.notes == [
        "base URL from `host`: not resolved (a local variable, a method or service discovery); "
        "read the caller source"
    ]


def test_a_client_built_on_the_spot_from_an_injected_builder():
    (fact,) = calls('''
        class VisitsClient {
            private String hostname = "http://visits-service/";
            private final WebClient.Builder webClientBuilder;
            Mono<Visits> visits(String ids) {
                return webClientBuilder.build().get()
                    .uri(hostname + "pets/visits?petId={petId}", ids)
                    .retrieve().bodyToMono(Visits.class);
            }
        }''')
    assert (fact.client, fact.http_method, fact.path, fact.query, fact.response) == (
        "web_client", "GET", "/pets/visits", ["petId"], "Visits"
    )
    assert fact.base_url == "http://visits-service/"


def test_web_client_create_with_a_literal_url():
    (fact,) = calls('''
        class Ping {
            void ping() { WebClient.create("http://status").get().uri("/health").retrieve(); }
        }''')
    assert (fact.client, fact.path) == ("web_client", "/health")
