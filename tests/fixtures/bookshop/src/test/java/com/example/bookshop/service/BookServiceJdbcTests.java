package com.example.bookshop.service;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

@SpringBootTest
@ActiveProfiles("jdbc")
class BookServiceJdbcTests extends AbstractBookServiceTests {
}
