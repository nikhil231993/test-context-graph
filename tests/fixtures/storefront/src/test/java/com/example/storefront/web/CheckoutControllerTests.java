package com.example.storefront.web;

import com.example.storefront.service.CheckoutService;

@WebMvcTest(CheckoutController.class)
class CheckoutControllerTests {
    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private CheckoutService checkoutService;

    @Test
    void checkoutReturnsTheStatus() throws Exception {
        mockMvc.perform(post("/api/checkout").param("sku", "A1").param("quantity", "2"))
                .andExpect(status().isOk());
    }
}
