import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type Item = { value: string; label: string }
export function ValueSelect({
  label,
  value,
  items,
  onChange,
  disabled,
  className,
}: {
  label: string
  value: string
  items: Item[]
  onChange: (value: string) => void
  disabled?: boolean
  className?: string
}) {
  return (
    <Select
      items={items}
      value={value}
      disabled={disabled}
      onValueChange={(next) => {
        if (next !== null) onChange(next)
      }}
    >
      <SelectTrigger aria-label={label} className={className}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
