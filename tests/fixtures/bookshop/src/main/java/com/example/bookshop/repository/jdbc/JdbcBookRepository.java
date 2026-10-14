package com.example.bookshop.repository.jdbc;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Repository;

import com.example.bookshop.model.Book;
import com.example.bookshop.repository.BookRepository;

@Repository
@Profile("jdbc")
public class JdbcBookRepository implements BookRepository {

    @Override
    public Optional<Book> findById(int id) {
        return Optional.empty();
    }

    @Override
    public Collection<Book> findAll() {
        return List.of();
    }

    @Override
    public void save(Book book) {
    }
}
