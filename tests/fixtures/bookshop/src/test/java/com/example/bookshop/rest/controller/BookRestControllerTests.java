package com.example.bookshop.rest.controller;

import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import com.example.bookshop.model.Book;
import com.example.bookshop.service.BookService;

@SpringBootTest
class BookRestControllerTests {

    @Autowired
    private BookRestController bookRestController;

    @MockitoBean
    private BookService bookService;

    private MockMvc mockMvc;

    @BeforeEach
    void setup() {
        mockMvc = MockMvcBuilders.standaloneSetup(bookRestController).build();
    }

    @Test
    void getBookReturnsTheBook() throws Exception {
        Book book = new Book();
        book.setId(1);
        given(bookService.findBook(1)).willReturn(book);
        mockMvc.perform(get("/api/books/1")).andExpect(status().isOk());
    }

    @Test
    void getBookReturnsNotFound() throws Exception {
        int bookId = 9;
        mockMvc.perform(get("/api/books/" + bookId)).andExpect(status().isNotFound());
    }
}
