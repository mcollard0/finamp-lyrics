using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Text.Json;

if (args.Length != 1) throw new ArgumentException("Supply a DLL path");
using var stream = File.OpenRead(args[0]);
using var pe = new PEReader(stream);
var metadata = pe.GetMetadataReader();
var assembly = metadata.GetAssemblyDefinition();
string? framework = null;
foreach (var handle in assembly.GetCustomAttributes())
{
    var attribute = metadata.GetCustomAttribute(handle);
    if (attribute.Constructor.Kind != HandleKind.MemberReference) continue;
    var member = metadata.GetMemberReference((MemberReferenceHandle)attribute.Constructor);
    if (member.Parent.Kind != HandleKind.TypeReference) continue;
    var type = metadata.GetTypeReference((TypeReferenceHandle)member.Parent);
    if (metadata.GetString(type.Namespace) != "System.Runtime.Versioning"
        || metadata.GetString(type.Name) != "TargetFrameworkAttribute") continue;
    var blob = metadata.GetBlobReader(attribute.Value);
    if (blob.ReadUInt16() != 1) throw new BadImageFormatException("Invalid framework attribute");
    framework = blob.ReadSerializedString();
}
Console.WriteLine(JsonSerializer.Serialize(new {
    name = metadata.GetString(assembly.Name), version = assembly.Version.ToString(), framework
}));
