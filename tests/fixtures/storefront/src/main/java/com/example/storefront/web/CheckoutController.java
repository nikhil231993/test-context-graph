package com.example.storefront.web;

import com.example.storefront.service.CheckoutService;

@RestController
@RequestMapping("/api")
public class CheckoutController {
    private final CheckoutService checkoutService;

    public CheckoutController(CheckoutService checkoutService) {
        this.checkoutService = checkoutService;
    }

    @PostMapping("/checkout")
    public ResponseEntity<String> checkout(@RequestParam String sku, @RequestParam int quantity) {
        return ResponseEntity.ok(checkoutService.checkout(sku, quantity));
    }
}
