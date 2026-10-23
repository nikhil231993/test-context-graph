package com.example.storefront.client;

@Component
public class StockClient {
    private final WebClient stockWebClient;

    public StockClient(WebClient stockWebClient) {
        this.stockWebClient = stockWebClient;
    }

    public void reserve(String sku, int quantity) {
        stockWebClient.post()
                .uri("/stock/{sku}/reservations", sku)
                .header("X-Request-Source", "storefront")
                .bodyValue(Map.of("quantity", quantity))
                .retrieve()
                .toBodilessEntity()
                .block();
    }
}
