package com.example.bookshop.service;

import java.util.Collection;

import com.example.bookshop.model.Book;

public interface BookService {

    Book findBook(int bookId);

    Collection<Book> findBooks();
}
