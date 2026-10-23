package com.example.storefront.config;

@Configuration
public class ClientConfig {
    @Bean
    public WebClient stockWebClient(WebClient.Builder builder, @Value("${stock.api.url}") String url) {
        return builder.baseUrl(url).build();
    }
}
