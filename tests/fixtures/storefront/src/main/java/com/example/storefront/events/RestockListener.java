package com.example.storefront.events;

import com.example.storefront.client.StockClient;

@Component
public class RestockListener {
    private final StockClient stockClient;

    public RestockListener(StockClient stockClient) {
        this.stockClient = stockClient;
    }

    @KafkaListener(topics = "restock-requests")
    public void onRestock(String sku) {
        stockClient.reserve(sku, 0);
    }
}
