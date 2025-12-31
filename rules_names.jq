def nz:(if .==null then "" else tostring end); 
def rules_names($nameTag;$awsName): {NameTag:($nameTag|nz),AwsGeneratedName:($awsName|nz)} | with_entries(select(.value!=""));
