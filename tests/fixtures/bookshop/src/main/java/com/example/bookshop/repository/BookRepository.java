package com.example.bookshop.repository;

import java.util.Collection;
import java.util.Optional;

import com.example.bookshop.model.Book;

public interface BookRepository {

    Optional<Book> findById(int id);

    Collection<Book> findAll();

    void save(Book book);
}
