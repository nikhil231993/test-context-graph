package com.example.bookshop.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

abstract class AbstractBookServiceTests {

    @Autowired
    protected BookService bookService;

    @Test
    void findBooksIsNeverNull() {
        assertThat(bookService.findBooks()).isNotNull();
    }
}
