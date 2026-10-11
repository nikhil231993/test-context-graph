package org.example.library.model;

public enum LoanStatus implements Labelled {
    OPEN("open"),
    RETURNED("returned") {
        @Override
        public boolean isClosed() {
            return true;
        }
    },
    LOST("lost");

    private final String label;

    LoanStatus(String label) {
        this.label = label;
    }

    public boolean isClosed() {
        return this == LOST;
    }

    public static LoanStatus fromLabel(String label) {
        for (LoanStatus status : values()) {
            if (status.label.equals(label)) {
                return status;
            }
        }
        return OPEN.next();
    }

    LoanStatus next() {
        return values()[(ordinal() + 1) % values().length];
    }
}
