from dataclasses import dataclass


@dataclass
class Exposure:
    capital: float
    max_fraction: float = 0.80

    def can_add(self, current_value: float, proposed_value: float) -> bool:
        return current_value >= 0 and proposed_value >= 0 and current_value + proposed_value <= self.capital * self.max_fraction
