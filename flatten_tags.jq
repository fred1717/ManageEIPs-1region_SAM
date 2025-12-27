def flatten_tags($tags): ($tags // [] | if length>0 then . else [] end);
