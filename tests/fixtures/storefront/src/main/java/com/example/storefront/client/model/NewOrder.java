package com.example.storefront.client.model;

public record NewOrder(String sku, int quantity, Address shipTo) {}
