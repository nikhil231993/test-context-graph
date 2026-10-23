package com.example.storefront.service;

import com.example.storefront.client.OrderClient;
import com.example.storefront.client.StockClient;
import com.example.storefront.client.model.Address;
import com.example.storefront.client.model.NewOrder;
import com.example.storefront.client.model.OrderReceipt;

@Service
public class CheckoutServiceImpl implements CheckoutService {
    private final OrderClient orderClient;
    private final StockClient stockClient;

    public CheckoutServiceImpl(OrderClient orderClient, StockClient stockClient) {
        this.orderClient = orderClient;
        this.stockClient = stockClient;
    }

    @Override
    public String checkout(String sku, int quantity) {
        stockClient.reserve(sku, quantity);
        OrderReceipt receipt = orderClient.placeOrder(UUID.randomUUID().toString(),
                new NewOrder(sku, quantity, new Address("Springfield")));
        return receipt.status();
    }
}
