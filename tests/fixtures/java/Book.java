package org.example.library.model;

import java.util.HashSet;
import java.util.Set;

import jakarta.persistence.*;
import jakarta.validation.constraints.NotEmpty;

/**
 * A book on the shelves.
 */
@Entity
@Table(name = "books", uniqueConstraints = @UniqueConstraint(
    columnNames = {"isbn", "edition"}))
public class Book extends BaseItem {

    @Column(name = "title", nullable = false)
    @NotEmpty
    private String title;

    @ManyToMany(fetch = FetchType.EAGER)
    @JoinTable(name = "book_authors", joinColumns = @JoinColumn(name = "book_id"),
        inverseJoinColumns = @JoinColumn(name = "author_id"))
    private Set<Author> authors = new HashSet<>();

    private int copies = 1, reserved;

    @Transient
    private static final String[] FORMATS = {"paper", "ebook"};

    @jakarta.persistence.Version
    private Integer version;

    public String getTitle() {
        return this.title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public void addAuthor(Author author) {
        getAuthorsInternal().add(author);
        author.getBooks().add(this);
    }

    protected Set<Author> getAuthorsInternal() {
        if (this.authors == null) {
            this.authors = new HashSet<>();
        }
        return this.authors;
    }
}
