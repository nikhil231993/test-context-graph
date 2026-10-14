package com.example.bookshop.model;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;

/** A book in the shop. */
@Entity
public class Book {

    @Id
    private Integer id;

    private String title;

    private String author;

    public Integer getId() {
        return id;
    }

    public void setId(Integer id) {
        this.id = id;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public boolean isByAuthor(String name) {
        return author != null && author.equalsIgnoreCase(name);
    }
}
