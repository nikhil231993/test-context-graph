package org.example.library.validation;

import jakarta.validation.Constraint;
import jakarta.validation.Payload;
import java.lang.annotation.*;

@Target({ ElementType.FIELD, ElementType.PARAMETER })
@Retention(RetentionPolicy.RUNTIME)
@Constraint(validatedBy = IsbnValidator.class)
@Documented
public @interface ValidIsbn {

    String message() default "Not a valid ISBN";

    Class<?>[] groups() default {};

    Class<? extends Payload>[] payload() default {};
}
