package com.example.storefront.client;

import com.example.storefront.client.model.NewOrder;
import com.example.storefront.client.model.OrderReceipt;

@FeignClient(name = "orders", url = "${orders.api.url}")
public interface OrderClient {
    @PostMapping("/orders")
    OrderReceipt placeOrder(@RequestHeader("Idempotency-Key") String key, @RequestBody NewOrder order);

    @GetMapping("/orders/{orderId}")
    OrderReceipt getOrder(@PathVariable("orderId") long orderId);
}
