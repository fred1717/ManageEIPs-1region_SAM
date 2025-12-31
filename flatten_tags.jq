def tv($k):((.Tags//[])|map(select(.Key==$k)|.Value)|.[0]//""); 
def nz:(if .==null then "" else tostring end); 
def flatten_tags($rid): {ResourceId:($rid|nz),NameTag:(tv("Name")|nz),Project:(tv("Project")|nz),Environment:(tv("Environment")|nz),Owner:(tv("Owner")|nz),ManagedBy:(tv("ManagedBy")|nz),CostCenter:(tv("CostCenter")|nz),Component:(tv("Component")|nz),GroupName:(tv("GroupName")|nz),RuleName:(tv("RuleName")|nz),AwsGeneratedName:(tv("aws:cloudformation:logical-id")|nz)} | with_entries(select(.value!=""));
