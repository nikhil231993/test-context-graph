package com.example.storefront;

@SpringBootApplication
@EnableFeignClients
public class StorefrontApplication {
    public static void main(String[] args) {
        SpringApplication.run(StorefrontApplication.class, args);
    }
}
