def _tags($o): ($o.Tags // $o.TagSet // []);
def tag_value($o; $k): (_tags($o) | first(.[]? | select(.Key==$k) | .Value) // null);
def tag_name($o): tag_value($o; "Name");
def must_tag_name($o): (tag_name($o) // "MISSING-NAME-TAG");
