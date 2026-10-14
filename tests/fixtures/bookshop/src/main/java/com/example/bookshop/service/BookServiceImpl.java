package com.example.bookshop.service;

import java.util.Collection;
import java.util.function.Supplier;

import org.springframework.stereotype.Service;

import com.example.bookshop.model.Book;
import com.example.bookshop.repository.BookRepository;

@Service
public class BookServiceImpl implements BookService {

    private final BookRepository bookRepository;

    public BookServiceImpl(BookRepository bookRepository) {
        this.bookRepository = bookRepository;
    }

    /** Finds a book, or returns null when there is none. */
    @Override
    public Book findBook(int bookId) {
        return find(() -> bookRepository.findById(bookId).orElse(null));
    }

    @Override
    public Collection<Book> findBooks() {
        return bookRepository.findAll();
    }

    private <T> T find(Supplier<T> lookup) {
        return lookup.get();
    }
}
